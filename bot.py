# pip install pyTelegramBotAPI SQLAlchemy psycopg2-binary APScheduler pytz requests
# В Railway.app укажи переменные окружения: BOT_TOKEN, DATABASE_URL, DEFAPI_KEY (опц.)x

import os
import io
import time
import datetime
import requests
import telebot
from telebot import types
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Date, ForeignKey
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
    user_id = Column(BigInteger, nullable=False)
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


class Assist(Base):
    __tablename__ = 'assists'
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey('matches.id'), nullable=False)
    player_username = Column(String, nullable=False)
    assists_count = Column(Integer, nullable=False, default=0)


class PlayerStat(Base):
    __tablename__ = 'player_stats'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False)
    month = Column(String, nullable=False)  # "2026-02"
    goals = Column(Integer, nullable=False, default=0)
    assists = Column(Integer, nullable=False, default=0)
    matches = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    player_points = Column(Integer, nullable=False, default=0)  # 3 за победу, 1 за ничью
    day_wins = Column(Integer, nullable=False, default=0)  # ручная корректировка дн.поб.


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

    # Миграция: player_points в player_stats
    try:
        _conn.execute(
            __import__('sqlalchemy').text(
                "ALTER TABLE player_stats ADD COLUMN player_points INTEGER NOT NULL DEFAULT 0"
            )
        )
        _conn.commit()
    except Exception:
        _conn.rollback()

    # Миграция: user_id INTEGER → BIGINT (Telegram ID-шники могут превышать 2^31)
    try:
        _conn.execute(
            __import__('sqlalchemy').text(
                "ALTER TABLE daily_votes ALTER COLUMN user_id TYPE BIGINT"
            )
        )
        _conn.commit()
    except Exception:
        _conn.rollback()  # уже BIGINT — ничего не делаем

    # Миграция: assists в player_stats
    try:
        _conn.execute(
            __import__('sqlalchemy').text(
                "ALTER TABLE player_stats ADD COLUMN assists INTEGER NOT NULL DEFAULT 0"
            )
        )
        _conn.commit()
    except Exception:
        _conn.rollback()

    # Миграция: day_wins в player_stats (ручная корректировка)
    try:
        _conn.execute(
            __import__('sqlalchemy').text(
                "ALTER TABLE player_stats ADD COLUMN day_wins INTEGER NOT NULL DEFAULT 0"
            )
        )
        _conn.commit()
    except Exception:
        _conn.rollback()

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
scheduler.add_job(send_thursday_poll, 'cron', day_of_week='thu', hour=10, minute=0)
scheduler.start()

# ─── Обработка ответов на poll ───────────────────────────────────────────────


@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    """Сохраняем голос пользователя (с ретраями при ошибке)."""
    user = poll_answer.user
    uid = user.id
    uname = user.username or user.first_name or str(uid)
    # Собираем реальное имя из профиля Telegram
    name_parts = [user.first_name or '', user.last_name or '']
    display_name = ' '.join(p for p in name_parts if p).strip() or None
    option_ids = poll_answer.option_ids

    for attempt in range(3):
        session = Session()
        try:
            if not option_ids:
                # Голос отозван — удаляем из списка и из команды
                vote = session.query(DailyVote).filter(
                    DailyVote.date == today(), DailyVote.user_id == uid
                ).first()
                if vote:
                    session.query(TeamToday).filter(
                        TeamToday.date == today(),
                        TeamToday.player_username == vote.username
                    ).delete()
                    session.delete(vote)
                session.commit()
                return

            choice = option_ids[0]
            # Удаляем старый голос за сегодня, если есть
            old_vote = session.query(DailyVote).filter(
                DailyVote.date == today(), DailyVote.user_id == uid
            ).first()
            if old_vote:
                # Если меняет на «не играю» (1) — убираем из команды тоже
                if choice == 1:
                    session.query(TeamToday).filter(
                        TeamToday.date == today(),
                        TeamToday.player_username == old_vote.username
                    ).delete()
                session.delete(old_vote)
            session.add(DailyVote(
                date=today(), user_id=uid, username=uname,
                display_name=display_name, choice=choice,
            ))
            session.commit()
            return  # успех — выходим
        except Exception as e:
            session.rollback()
            print(f'[PollAnswer] Attempt {attempt+1} failed for uid={uid}: {e}')
            if attempt < 2:
                time.sleep(1)
        finally:
            session.close()
    print(f'[PollAnswer] FAILED to save vote for uid={uid} uname={uname} after 3 attempts')


# ─── Команды бота ────────────────────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
def cmd_help(msg):
    text = (
        "Бот для организации игр\n\n"
        "Команды админа:\n"
        "/players — игроки на сегодня\n"
        "/add_player Имя — вручную добавить игрока\n"
        "/edit_player @user Имя — изменить имя игрока\n"
        "/remove_player @user — убрать игрока\n"
        "/clear_today — очистить данные дня\n"
        "/set_teams N — задать кол-во команд (2–6)\n"
        "/set_team_name N Имя — название команды\n"
        "/add_to_team  — добавить в команду\n"
        "/teams — показать составы\n"
        "/record — записать матч\n"
        "/adjust_stats @user [поле] [значение]\n"
        "  Поля: rating, matches\n"
        "  Пример: /adjust_stats @user rating +3\n"
        "/auto_teams N — авто-распределение по рейтингу\n"
        "/reset_month — обнулить статистику месяца\n"
        "/poll — отправить poll вручную\n"
        "/mvp — poll MVP дня\n"
        "/stats_day_img — день картинкой\n"
        "/stats_month_img — команды картинкой\n"
        "/rating_img — рейтинг картинкой\n\n"
        "Для всех:\n"
        "/stats — вся статистика\n"
        "/stats_day — статистика дня\n"
        "/stats_month — статистика месяца\n"
        "/rating — рейтинг игроков\n\n"
        "Рейтинг: 1-е место за день = 3 очка, "
        "2-е = 2, 3-е = 1, остальные = 0\n"
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


@bot.message_handler(commands=['add_player'])
def cmd_add_player(msg):
    """Вручную добавить игрока в список на сегодня (если голос потерялся)."""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, 'Формат: /add_player Имя Фамилия\nИли: /add_player @username Имя Фамилия')
        return
    raw = parts[1].strip()
    # Парсим: либо "@username Имя Фамилия", либо просто "Имя Фамилия"
    tokens = raw.split()
    if tokens[0].startswith('@'):
        uname = tokens[0].lstrip('@')
        display_name = ' '.join(tokens[1:]) if len(tokens) > 1 else None
    else:
        # Нет username — используем имя как username (транслит/как есть)
        display_name = raw
        uname = raw.replace(' ', '_').lower()
    session = Session()
    try:
        # Проверяем, не дублируется ли
        existing = session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.username == uname
        ).first()
        if existing:
            bot.reply_to(msg, f'@{uname} уже в списке на сегодня.')
            return
        session.add(DailyVote(
            date=today(), user_id=0, username=uname,
            display_name=display_name, choice=0,
        ))
        session.commit()
        label = f'{display_name} (@{uname})' if display_name else f'@{uname}'
        bot.reply_to(msg, f'{label} добавлен в список на сегодня.')
    finally:
        session.close()


@bot.message_handler(commands=['edit_player'])
def cmd_edit_player(msg):
    """Изменить имя игрока. Формат: /edit_player @username Новое Имя"""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].startswith('@'):
        bot.reply_to(msg, 'Формат: /edit_player @username Новое Имя')
        return
    uname = parts[1].lstrip('@')
    new_display = parts[2].strip()
    session = Session()
    try:
        vote = session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.username == uname
        ).first()
        if not vote:
            bot.reply_to(msg, f'@{uname} нет в списке на сегодня.')
            return
        old_display = vote.display_name or '(не задано)'
        vote.display_name = new_display
        session.commit()
        bot.reply_to(msg, f'@{uname}: «{old_display}» → «{new_display}»')
    finally:
        session.close()


@bot.message_handler(commands=['remove_player'])
def cmd_remove_player(msg):
    """Убрать игрока из списка на сегодня. Формат: /remove_player @username"""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, 'Формат: /remove_player @username')
        return
    uname = parts[1].lstrip('@')
    session = Session()
    try:
        vote = session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.username == uname
        ).first()
        if not vote:
            bot.reply_to(msg, f'@{uname} нет в списке на сегодня.')
            return
        dn = vote.display_name
        label = f'{dn} (@{uname})' if dn else f'@{uname}'
        # Убираем из команды, если был назначен
        session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username == uname
        ).delete()
        session.delete(vote)
        session.commit()
        bot.reply_to(msg, f'{label} убран из списка на сегодня.')
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
            session.query(Assist).filter(Assist.match_id.in_(today_match_ids)).delete()
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


# ─── /add_to_team — interactive team assignment ─────────────────────────────

def _get_unassigned_players(session):
    players = get_today_players(session)
    assigned = session.query(TeamToday.player_username).filter(
        TeamToday.date == today(),
        TeamToday.player_username != '__placeholder__'
    ).all()
    assigned_set = {a[0] for a in assigned}
    return [p for p in players if p.username not in assigned_set]


def _build_team_summary(session):
    dn = get_display_names(session)
    teams = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.player_username != '__placeholder__'
    ).order_by(TeamToday.team_number).all()
    if not teams:
        return 'Пока никто не распределён.'
    from collections import defaultdict
    grouped = defaultdict(list)
    tnames = {}
    for t in teams:
        grouped[t.team_number].append(t.player_username)
        tnames[t.team_number] = t.team_name
    lines = []
    for num in sorted(grouped.keys()):
        name = tnames.get(num, f'Команда {num}')
        parts = []
        for u in grouped[num]:
            real = dn.get(u)
            parts.append(f'{real} (@{u})' if real else f'@{u}')
        lines.append(f'#{num} «{name}»: {", ".join(parts)}')
    return '\n'.join(lines)


def _send_player_selection(chat_id, uid, state):
    session = Session()
    try:
        unassigned = _get_unassigned_players(session)
        dn = get_display_names(session)
        state['display_names'] = dn
        summary = _build_team_summary(session)
    finally:
        session.close()
    if not unassigned:
        bot.send_message(chat_id, f'Все игроки распределены!\n\n{summary}')
        if uid in user_states:
            del user_states[uid]
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for p in unassigned:
        real = dn.get(p.username)
        label = f'{real} (@{p.username})' if real else f'@{p.username}'
        buttons.append(types.InlineKeyboardButton(
            label, callback_data=f'att_player:{p.username}'
        ))
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i + 2])
    markup.row(types.InlineKeyboardButton('✅ Готово', callback_data='att_done'))
    text = f'Текущие составы:\n{summary}\n\nНераспределённых: {len(unassigned)}\nВыбери игрока:'
    bot.send_message(chat_id, text, reply_markup=markup)


def _send_team_selection(chat_id, state, username):
    dn = state.get('display_names', {})
    real = dn.get(username)
    player_label = f'{real} (@{username})' if real else f'@{username}'
    team_names = state['team_names']
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for num in sorted(team_names.keys()):
        buttons.append(types.InlineKeyboardButton(
            f'{num}. {team_names[num]}',
            callback_data=f'att_team:{num}:{username}'
        ))
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i + 2])
    markup.row(types.InlineKeyboardButton('⬅️ Назад', callback_data='att_back'))
    bot.send_message(chat_id, f'В какую команду добавить {player_label}?', reply_markup=markup)


@bot.message_handler(commands=['add_to_team'])
def cmd_add_to_team(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        team_names = get_team_names_today(session)
        if not team_names:
            bot.reply_to(msg, 'Сначала создай команды: /set_teams')
            return
        players = get_today_players(session)
        if not players:
            bot.reply_to(msg, 'Сегодня пока никто не записался.')
            return
        dn = get_display_names(session)
    finally:
        session.close()
    if msg.from_user.id in user_states:
        del user_states[msg.from_user.id]
    state = {
        'mode': 'add_to_team',
        'team_names': team_names,
        'display_names': dn,
    }
    user_states[msg.from_user.id] = state
    _send_player_selection(msg.chat.id, msg.from_user.id, state)


@bot.callback_query_handler(func=lambda call: call.data.startswith('att_'))
def handle_add_to_team_callback(call):
    uid = call.from_user.id
    if uid not in user_states or user_states[uid].get('mode') != 'add_to_team':
        bot.answer_callback_query(call.id, 'Сессия не активна. Используй /add_to_team')
        return
    state = user_states[uid]
    data = call.data

    if data == 'att_done':
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        session = Session()
        try:
            summary = _build_team_summary(session)
        finally:
            session.close()
        bot.send_message(call.message.chat.id, f'Распределение завершено!\n\n{summary}')
        del user_states[uid]

    elif data == 'att_back':
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        _send_player_selection(call.message.chat.id, uid, state)

    elif data.startswith('att_player:'):
        username = data.split(':', 1)[1]
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        _send_team_selection(call.message.chat.id, state, username)

    elif data.startswith('att_team:'):
        parts = data.split(':', 2)
        team_num = int(parts[1])
        username = parts[2]
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        session = Session()
        try:
            team_names = state['team_names']
            team_name = team_names.get(team_num, f'Команда {team_num}')
            session.query(TeamToday).filter(
                TeamToday.date == today(),
                TeamToday.player_username == username
            ).delete()
            session.add(TeamToday(
                date=today(), team_number=team_num,
                team_name=team_name, player_username=username
            ))
            session.commit()
            dn = get_display_names(session)
            state['display_names'] = dn
            real = dn.get(username)
            label = f'{real} (@{username})' if real else f'@{username}'
        finally:
            session.close()
        bot.send_message(
            call.message.chat.id,
            f'✅ {label} → #{team_num} «{team_names.get(team_num, "")}»'
        )
        _send_player_selection(call.message.chat.id, uid, state)



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
    user_states[msg.from_user.id] = {'mode': 'record', 'step': 1, 'teams': names}

    bot.reply_to(msg, 'Шаг 1: Выбери команду A:', reply_markup=markup)






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


def _send_assists_keyboard(chat_id, state, text):
    """Отправляет inline-клавиатуру с игроками для записи ассистов."""
    dn = state.get('display_names', {})
    players = state.get('match_players', [])
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for u in players:
        real = dn.get(u)
        label = f'{real} (@{u})' if real else f'@{u}'
        buttons.append(types.InlineKeyboardButton(label, callback_data=f'assist_player:{u}'))
    for i in range(0, len(buttons), 2):
        markup.row(*buttons[i:i+2])
    markup.row(types.InlineKeyboardButton('Готово', callback_data='assist_done'))

    # Текущие ассисты
    if state.get('assists'):
        tally = []
        for a in state['assists']:
            real = dn.get(a['username'])
            name = f'{real} (@{a["username"]})' if real else f'@{a["username"]}'
            tally.append(f'  {name}: {a["count"]} асс.')
        text += '\n\nЗаписаны ассисты:\n' + '\n'.join(tally)

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
        # Переход к записи ассистов (шаг 5)
        state['step'] = 5
        state['assists'] = []
        _send_assists_keyboard(
            call.message.chat.id, state,
            'Кто отдал ассист? Нажми на игрока (или Готово, если ассистов нет):'
        )

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


@bot.callback_query_handler(func=lambda call: call.data.startswith('assist_'))
def handle_assist_callback(call):
    uid = call.from_user.id
    if uid not in user_states or user_states[uid].get('step') != 5:
        bot.answer_callback_query(call.id, 'Сессия записи не активна.')
        return
    state = user_states[uid]
    data = call.data

    if data == 'assist_done':
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        if state.get('mode') == 'add_goals':
            _finish_add_goals(call.message.chat.id, state)
        else:
            _finish_match(call.message, state)
        del user_states[uid]

    elif data.startswith('assist_player:'):
        username = data.split(':', 1)[1]
        state['awaiting_assist_count_for'] = username
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        dn = state.get('display_names', {})
        real = dn.get(username)
        label = f'{real} (@{username})' if real else f'@{username}'
        bot.send_message(
            call.message.chat.id,
            f'Сколько ассистов у {label}? Введи число:',
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

        # Обновляем кол-во матчей игроков
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
    """Обновляем кол-во матчей для всех игроков команд A и B."""
    team_a_num = state['team_a']
    team_b_num = state['team_b']

    all_team_players = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.team_number.in_([team_a_num, team_b_num]),
        TeamToday.player_username != '__placeholder__'
    ).all()

    for tp in all_team_players:
        stat = session.query(PlayerStat).filter(
            PlayerStat.username == tp.player_username, PlayerStat.month == month
        ).first()
        if not stat:
            stat = PlayerStat(username=tp.player_username, month=month, matches=0)
            session.add(stat)
        stat.matches = (stat.matches or 0) + 1


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


# ─── /add_goals — добавить голы к существующему матчу ─────────────────────────

@bot.message_handler(commands=['add_goals'])
def cmd_add_goals(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        today_matches = session.query(Match).filter(Match.date == today()).all()
        if not today_matches:
            bot.reply_to(msg, 'Сегодня матчей ещё не было.')
            return
        team_names = get_team_names_today(session)
        markup = types.InlineKeyboardMarkup(row_width=1)
        for m in today_matches:
            na = team_names.get(m.team_a_num, f'#{m.team_a_num}')
            nb = team_names.get(m.team_b_num, f'#{m.team_b_num}')
            label = f'{na} {m.score_a}:{m.score_b} {nb}'
            markup.add(types.InlineKeyboardButton(label, callback_data=f'addg_match:{m.id}'))
        bot.reply_to(msg, 'Выбери матч для добавления голов:', reply_markup=markup)
    finally:
        session.close()


@bot.callback_query_handler(func=lambda call: call.data.startswith('addg_'))
def handle_add_goals_callback(call):
    uid = call.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(call.id, 'Только для админов.')
        return
    data = call.data

    if data.startswith('addg_match:'):
        match_id = int(data.split(':')[1])
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        session = Session()
        try:
            match = session.query(Match).filter(Match.id == match_id).first()
            if not match:
                bot.send_message(call.message.chat.id, 'Матч не найден.')
                return
            team_names = get_team_names_today(session)
            dn = get_display_names(session)
            team_players = session.query(TeamToday).filter(
                TeamToday.date == today(),
                TeamToday.team_number.in_([match.team_a_num, match.team_b_num]),
                TeamToday.player_username != '__placeholder__'
            ).all()
            match_players = [t.player_username for t in team_players]
        finally:
            session.close()
        na = team_names.get(match.team_a_num, f'#{match.team_a_num}')
        nb = team_names.get(match.team_b_num, f'#{match.team_b_num}')
        state = {
            'mode': 'add_goals',
            'step': 4,
            'match_id': match_id,
            'match_month': match.month,
            'match_players': match_players,
            'display_names': dn,
            'goals': [],
        }
        user_states[uid] = state
        _send_goals_keyboard(
            call.message.chat.id, state,
            f'Матч: {na} {match.score_a}:{match.score_b} {nb}\nКто забил? Нажми на игрока:'
        )

    elif data == 'addg_done':
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        if uid not in user_states or user_states[uid].get('mode') != 'add_goals':
            return
        state = user_states[uid]
        _finish_add_goals(call.message.chat.id, state)
        del user_states[uid]


def _finish_add_goals(chat_id, state):
    """Сохраняем дополнительные голы и ассисты к существующему матчу."""
    session = Session()
    try:
        match_id = state['match_id']
        month = state['match_month']
        for g in state.get('goals', []):
            session.add(Goal(
                match_id=match_id,
                player_username=g['username'],
                goals_count=g['count'],
            ))
            # Обновляем player_stats
            stat = session.query(PlayerStat).filter(
                PlayerStat.username == g['username'],
                PlayerStat.month == month
            ).first()
            if stat:
                stat.goals += g['count']
            else:
                stat = PlayerStat(
                    username=g['username'], month=month,
                    goals=g['count'], assists=0, matches=0, wins=0, player_points=0
                )
                session.add(stat)
        for a in state.get('assists', []):
            session.add(Assist(
                match_id=match_id,
                player_username=a['username'],
                assists_count=a['count'],
            ))
            # Обновляем player_stats
            stat = session.query(PlayerStat).filter(
                PlayerStat.username == a['username'],
                PlayerStat.month == month
            ).first()
            if stat:
                stat.assists += a['count']
            else:
                stat = PlayerStat(
                    username=a['username'], month=month,
                    goals=0, assists=a['count'], matches=0, wins=0, player_points=0
                )
                session.add(stat)
        session.commit()
        total_goals = sum(g['count'] for g in state.get('goals', []))
        total_assists = sum(a['count'] for a in state.get('assists', []))
        parts = []
        if total_goals:
            parts.append(f'{total_goals} гол.')
        if total_assists:
            parts.append(f'{total_assists} асс.')
        bot.send_message(chat_id, f'Добавлено {", ".join(parts) if parts else "0"} к матчу.')
    finally:
        session.close()


# ─── /remove_goals — удалить голы из матча ────────────────────────────────────

@bot.message_handler(commands=['remove_goals'])
def cmd_remove_goals(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        today_matches = session.query(Match).filter(Match.date == today()).all()
        if not today_matches:
            bot.reply_to(msg, 'Сегодня матчей ещё не было.')
            return
        team_names = get_team_names_today(session)
        markup = types.InlineKeyboardMarkup(row_width=1)
        for m in today_matches:
            na = team_names.get(m.team_a_num, f'#{m.team_a_num}')
            nb = team_names.get(m.team_b_num, f'#{m.team_b_num}')
            label = f'{na} {m.score_a}:{m.score_b} {nb}'
            markup.add(types.InlineKeyboardButton(label, callback_data=f'rmg_match:{m.id}'))
        bot.reply_to(msg, 'Выбери матч для удаления голов:', reply_markup=markup)
    finally:
        session.close()


def _send_remove_goals_keyboard(chat_id, match_id):
    """Показывает записанные голы матча как кнопки для удаления."""
    session = Session()
    try:
        match = session.query(Match).filter(Match.id == match_id).first()
        if not match:
            bot.send_message(chat_id, 'Матч не найден.')
            return
        team_names = get_team_names_today(session)
        na = team_names.get(match.team_a_num, f'#{match.team_a_num}')
        nb = team_names.get(match.team_b_num, f'#{match.team_b_num}')
        goals = session.query(Goal).filter(Goal.match_id == match_id).all()
        dn = get_display_names(session)
        if not goals:
            bot.send_message(chat_id, f'Матч {na} {match.score_a}:{match.score_b} {nb}\nГолов не записано.')
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for g in goals:
            real = dn.get(g.player_username)
            label = f'{real} ({g.goals_count} гол.)' if real else f'@{g.player_username} ({g.goals_count} гол.)'
            markup.add(types.InlineKeyboardButton(
                f'❌ {label}', callback_data=f'rmg_del:{g.id}:{match_id}'
            ))
        markup.add(types.InlineKeyboardButton('Готово', callback_data=f'rmg_done'))
        bot.send_message(
            chat_id,
            f'Матч: {na} {match.score_a}:{match.score_b} {nb}\nНажми чтобы удалить:',
            reply_markup=markup
        )
    finally:
        session.close()


@bot.callback_query_handler(func=lambda call: call.data.startswith('rmg_'))
def handle_remove_goals_callback(call):
    uid = call.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(call.id, 'Только для админов.')
        return
    data = call.data

    if data.startswith('rmg_match:'):
        match_id = int(data.split(':')[1])
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        _send_remove_goals_keyboard(call.message.chat.id, match_id)

    elif data.startswith('rmg_del:'):
        parts = data.split(':')
        goal_id = int(parts[1])
        match_id = int(parts[2])
        session = Session()
        try:
            goal = session.query(Goal).filter(Goal.id == goal_id).first()
            if goal:
                # Вычитаем голы из player_stats
                match = session.query(Match).filter(Match.id == match_id).first()
                month = match.month if match else current_month()
                stat = session.query(PlayerStat).filter(
                    PlayerStat.username == goal.player_username,
                    PlayerStat.month == month
                ).first()
                if stat:
                    stat.goals = max(0, stat.goals - goal.goals_count)
                session.delete(goal)
                session.commit()
                bot.answer_callback_query(call.id, 'Удалено')
            else:
                bot.answer_callback_query(call.id, 'Уже удалено')
        finally:
            session.close()
        bot.delete_message(call.message.chat.id, call.message.message_id)
        _send_remove_goals_keyboard(call.message.chat.id, match_id)

    elif data == 'rmg_done':
        bot.answer_callback_query(call.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, 'Готово.')


# ─── /adjust_stats — корректировка статистики игрока ──────────────────────────

ADJUSTABLE_FIELDS = {
    'rating': ('player_points', 'рейтинг (ручн.)'),
    'matches': ('matches', 'матчи'),
}


@bot.message_handler(commands=['adjust_stats'])
def cmd_adjust_stats(msg):
    """Корректировка статистики игрока за текущий месяц.
    /adjust_stats @username                — показать текущие значения
    /adjust_stats @username rating +3      — добавить к рейтингу
    /adjust_stats @username matches 10     — установить матчи
    Поля: rating, matches
    """
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2:
        fields = ', '.join(ADJUSTABLE_FIELDS.keys())
        bot.reply_to(
            msg,
            'Формат:\n'
            f'/adjust_stats @user — показать стат\n'
            f'/adjust_stats @user <поле> <значение>\n'
            f'Поля: {fields}\n'
            f'Значение: 5 (установить), +3 (добавить), -2 (убрать)'
        )
        return
    uname = parts[1].lstrip('@')
    session = Session()
    try:
        month = current_month()
        stat = session.query(PlayerStat).filter(
            PlayerStat.username == uname, PlayerStat.month == month
        ).first()
        dn = get_display_names(session)
        real = dn.get(uname)
        label = f'{real} (@{uname})' if real else f'@{uname}'

        # Если только username — показываем текущие значения
        if len(parts) == 2:
            computed = _compute_match_day_ratings(session, month)
            auto_pts = computed.get(uname, 0)
            manual_adj = stat.player_points if stat else 0
            matches_cnt = stat.matches if stat else 0
            total_rating = auto_pts + manual_adj
            bot.reply_to(
                msg,
                f'{label} ({month}):\n'
                f'  Рейтинг: {total_rating} (авто: {auto_pts}, ручн.: {manual_adj})\n'
                f'  Матчи: {matches_cnt}'
            )
            return

        if len(parts) < 4:
            bot.reply_to(msg, 'Формат: /adjust_stats @user <поле> <значение>')
            return

        field_key = parts[2].lower()
        val_str = parts[3]

        if field_key not in ADJUSTABLE_FIELDS:
            fields = ', '.join(ADJUSTABLE_FIELDS.keys())
            bot.reply_to(msg, f'Неизвестное поле. Доступные: {fields}')
            return

        attr_name, field_label = ADJUSTABLE_FIELDS[field_key]

        if not stat:
            stat = PlayerStat(username=uname, month=month)
            session.add(stat)

        old_val = getattr(stat, attr_name)
        if val_str.startswith('+'):
            new_val = old_val + int(val_str[1:])
        elif val_str.startswith('-'):
            new_val = max(0, old_val - int(val_str[1:]))
        else:
            new_val = max(0, int(val_str))

        setattr(stat, attr_name, new_val)
        session.commit()
        bot.reply_to(msg, f'{label}: {field_label} {old_val} → {new_val} ({month})')
    except ValueError:
        bot.reply_to(msg, 'Некорректное число.')
    finally:
        session.close()


# ─── /rating — рейтинг игроков ────────────────────────────────────────────────

def _compute_match_day_ratings(session, month):
    """Подсчитывает рейтинговые очки на основе места команды в дневной таблице.
    1-е место: 3 очка, 2-е: 2 очка, 3-е: 1 очко, 4+: 0.
    Возвращает {username: total_points}."""
    from collections import defaultdict
    matches = session.query(Match).filter(Match.month == month).all()
    if not matches:
        return {}
    by_date = defaultdict(list)
    for m in matches:
        by_date[m.date].append(m)

    player_ratings = defaultdict(int)
    placement_points = {0: 3, 1: 2, 2: 1}  # rank → очки

    for match_date, day_matches in by_date.items():
        team_points = defaultdict(int)
        team_gf = defaultdict(int)
        team_ga = defaultdict(int)
        for m in day_matches:
            if m.score_a > m.score_b:
                team_points[m.team_a_num] += 3
            elif m.score_a < m.score_b:
                team_points[m.team_b_num] += 3
            else:
                team_points[m.team_a_num] += 1
                team_points[m.team_b_num] += 1
            team_gf[m.team_a_num] += m.score_a
            team_ga[m.team_a_num] += m.score_b
            team_gf[m.team_b_num] += m.score_b
            team_ga[m.team_b_num] += m.score_a

        if not team_points:
            continue

        sorted_teams = sorted(
            team_points.keys(),
            key=lambda t: (team_points[t], team_gf[t], -team_ga[t]),
            reverse=True
        )

        # Раздаём очки по местам (с учётом ничьих)
        rank = 0
        i = 0
        while i < len(sorted_teams):
            cur = sorted_teams[i]
            cur_key = (team_points[cur], team_gf[cur], team_ga[cur])
            tied = []
            j = i
            while j < len(sorted_teams):
                t = sorted_teams[j]
                if (team_points[t], team_gf[t], team_ga[t]) == cur_key:
                    tied.append(t)
                    j += 1
                else:
                    break
            pts = placement_points.get(rank, 0)
            team_players = session.query(TeamToday).filter(
                TeamToday.date == match_date,
                TeamToday.team_number.in_(tied),
                TeamToday.player_username != '__placeholder__'
            ).all()
            for tp in team_players:
                player_ratings[tp.player_username] += pts
            rank += len(tied)
            i = j

    return dict(player_ratings)


def _get_player_ratings(session, month=None):
    """Возвращает список отсортированный по убыванию рейтинга.
    rating = placement_points(авто) + player_points(ручн. коррект.)"""
    if month is None:
        month = current_month()
    computed = _compute_match_day_ratings(session, month)
    stats = session.query(PlayerStat).filter(PlayerStat.month == month).all()
    stats_map = {s.username: s for s in stats}
    all_players = set(computed.keys()) | set(stats_map.keys())
    ratings = []
    for uname in all_players:
        comp_pts = computed.get(uname, 0)
        manual_adj = stats_map[uname].player_points if uname in stats_map else 0
        matches = stats_map[uname].matches if uname in stats_map else 0
        rating = comp_pts + manual_adj
        ratings.append((uname, rating, matches))
    ratings.sort(key=lambda x: x[1], reverse=True)
    return ratings


@bot.message_handler(commands=['rating'])
def cmd_rating(msg):
    session = Session()
    try:
        month = current_month()
        ratings = _get_player_ratings(session, month)
        dn = get_display_names(session)
        if not ratings:
            bot.reply_to(msg, 'Рейтинг пока пуст.')
            return
        lines = [f'Рейтинг игроков за {month}', '━' * 24]
        for i, (uname, rating, matches) in enumerate(ratings, 1):
            real = dn.get(uname)
            name = f'{real} (@{uname})' if real else f'@{uname}'
            lines.append(f'  {i}. {name} — {rating} очк. ({matches} матч.)')
        bot.reply_to(msg, '\n'.join(lines))
    finally:
        session.close()


# ─── /auto_teams — автоматическое распределение по рейтингу ──────────────────

@bot.message_handler(commands=['auto_teams'])
def cmd_auto_teams(msg):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(msg, 'Формат: /auto_teams N (2–6)')
        return
    n = int(parts[1])
    if n < 2 or n > 6:
        bot.reply_to(msg, 'Кол-во команд: от 2 до 6.')
        return
    session = Session()
    try:
        players = get_today_players(session)
        if not players:
            bot.reply_to(msg, 'Сегодня пока никто не записался.')
            return
        if len(players) < n:
            bot.reply_to(msg, f'Игроков ({len(players)}) меньше, чем команд ({n}).')
            return

        # Получаем рейтинги
        month = current_month()
        ratings = _get_player_ratings(session, month)
        rating_map = {uname: rating for uname, rating, _ in ratings}
        dn = get_display_names(session)

        # Сортируем сегодняшних игроков по рейтингу (убыв.), новички с рейтингом 0
        player_list = sorted(
            [p.username for p in players],
            key=lambda u: rating_map.get(u, 0),
            reverse=True
        )

        # Snake draft: раунд 1 — forward, все остальные — reverse
        teams = {i: [] for i in range(1, n + 1)}
        team_order = list(range(1, n + 1))  # [1, 2, 3]
        reverse_order = list(reversed(team_order))  # [3, 2, 1]

        idx = 0
        round_num = 0
        while idx < len(player_list):
            order = team_order if round_num == 0 else reverse_order
            for team_num in order:
                if idx >= len(player_list):
                    break
                teams[team_num].append(player_list[idx])
                idx += 1
            round_num += 1

        # Удаляем старые команды и создаём новые
        session.query(TeamToday).filter(TeamToday.date == today()).delete()
        for i in range(1, n + 1):
            team_name = DEFAULT_TEAM_NAMES[i - 1]
            # Placeholder для пустых команд
            session.add(TeamToday(
                date=today(), team_number=i,
                team_name=team_name, player_username='__placeholder__'
            ))
            for username in teams[i]:
                session.add(TeamToday(
                    date=today(), team_number=i,
                    team_name=team_name, player_username=username
                ))
        session.commit()

        # Вывод результата
        lines = [f'Авто-распределение ({n} команд, {len(player_list)} игроков):']
        lines.append('')
        for i in range(1, n + 1):
            team_name = DEFAULT_TEAM_NAMES[i - 1]
            parts_list = []
            for u in teams[i]:
                real = dn.get(u)
                r = rating_map.get(u, 0)
                label = f'{real} ({r})' if real else f'@{u} ({r})'
                parts_list.append(label)
            lines.append(f'#{i} «{team_name}»: {", ".join(parts_list)}')
        bot.reply_to(msg, '\n'.join(lines))
    finally:
        session.close()


# ─── /stats ──────────────────────────────────────────────────────────────────

def _build_day_stats_text(session):
    """Статистика за сегодняшний игровой день."""
    dn = get_display_names(session)
    team_names = get_team_names_today(session)
    lines = [f'Игровой день {today()}']
    lines.append('━' * 24)

    # Матчи сегодня
    today_matches = session.query(Match).filter(Match.date == today()).all()

    # Таблица дня (очки)
    if today_matches:
        from collections import defaultdict
        day_points = defaultdict(lambda: {'points': 0, 'goals_scored': 0, 'goals_conceded': 0})
        for m in today_matches:
            na = team_names.get(m.team_a_num, f'Команда {m.team_a_num}')
            nb = team_names.get(m.team_b_num, f'Команда {m.team_b_num}')
            day_points[na]['goals_scored'] += m.score_a
            day_points[na]['goals_conceded'] += m.score_b
            day_points[nb]['goals_scored'] += m.score_b
            day_points[nb]['goals_conceded'] += m.score_a
            if m.score_a > m.score_b:
                day_points[na]['points'] += 3
            elif m.score_a < m.score_b:
                day_points[nb]['points'] += 3
            else:
                day_points[na]['points'] += 1
                day_points[nb]['points'] += 1

            sorted_teams = sorted(day_points.items(), key=lambda x: (
                x[1]['points'],
                x[1]['goals_scored'],
                -x[1]['goals_conceded']
            ), reverse=True)

        lines.append('')
        lines.append('Таблица дня:')
        for tname, data in sorted_teams:
            lines.append(
                f'  {tname}: {data["points"]} очк. | '
                f'{data["goals_scored"]} заб. | {data["goals_conceded"]} проп.'
            )

    # Матчи
    if today_matches:
        lines.append('')
        lines.append('Матчи:')
        for m in today_matches:
            na = team_names.get(m.team_a_num, f'Команда {m.team_a_num}')
            nb = team_names.get(m.team_b_num, f'Команда {m.team_b_num}')
            lines.append(f'  {na}  {m.score_a} : {m.score_b}  {nb}')

    # Составы (в конце)
    team_rows = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.player_username != '__placeholder__'
    ).order_by(TeamToday.team_number).all()
    if team_rows:
        from collections import defaultdict as dd
        grouped = dd(list)
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
    ).all()
    teams.sort(key=lambda t: (t.points, t.goals_scored, -t.goals_conceded), reverse=True)
    if teams:
        lines.append('')
        lines.append('Команды:')
        for t in teams:
            tname = t.team_num_or_name.split('_', 1)[1] if '_' in t.team_num_or_name else t.team_num_or_name
            lines.append(
                f'  {tname}: {t.points} очк. | '
                f'{t.goals_scored} заб. | {t.goals_conceded} проп.'
            )

    # Игроки (все, отсортированные по рейтингу)
    ratings = _get_player_ratings(session, month)
    if ratings:
        lines.append('')
        lines.append('Игроки:')
        for i, (uname, rating, matches) in enumerate(ratings, 1):
            real = dn.get(uname)
            name = f'{real} (@{uname})' if real else f'@{uname}'
            lines.append(f'  {i}. {name} — {rating} очк. ({matches} матч.)')

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
                    return item if isinstance(item, str) else (item.get('url') or item.get('image'))
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
        "Create a professional football league statistics board. "
        "Dark background with gradient from dark blue to black. "
        "Style it like a real football league table display seen on TV broadcasts. "
        "Use a structured table layout with columns, rows, and grid lines. "
        "Add a golden trophy icon or football emblem at the top as a logo. "
        "Use team color badges/shields next to team names. "
        "Numbers should be in bold white, headers in golden/yellow color. "
        "Make it look like an official Premier League or Champions League stats screen. "
        "Include ALL the following data EXACTLY as written, formatted into proper table rows:\n\n"
        f"{stats_text}\n\n"
        "Layout rules: "
        "- Title at the top in large bold font "
        "- Team standings in a proper table with alternating row colors "
        "- Match results in a scoreboard style with team names on each side and score in the center "
        "- Team rosters at the bottom in a compact card style "
        "- All text must be perfectly readable, crisp and exact as provided above"
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
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        text = _build_day_stats_text(session)
    finally:
        session.close()
    _send_stats_image(msg, text, f'Игровой день {today()}')


@bot.message_handler(commands=['stats_month_img'])
def cmd_stats_month_img(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        month = current_month()
        dn = get_display_names(session)
        lines = [f'Команды за {month}']
        lines.append('━' * 24)
        teams = session.query(TeamStat).filter(TeamStat.month == month).all()
        teams.sort(key=lambda t: (t.points, t.goals_scored, -t.goals_conceded), reverse=True)
        if teams:
            for t in teams:
                tname = t.team_num_or_name.split('_', 1)[1] if '_' in t.team_num_or_name else t.team_num_or_name
                lines.append(
                    f'  {tname}: {t.points} очк. | '
                    f'{t.goals_scored} заб. | {t.goals_conceded} проп.'
                )
        text = '\n'.join(lines) if len(lines) > 2 else None
    finally:
        session.close()
    _send_stats_image(msg, text, f'Команды за {current_month()}')


@bot.message_handler(commands=['rating_img'])
def cmd_rating_img(msg):
    if not is_admin(msg.from_user.id):
        return
    session = Session()
    try:
        month = current_month()
        dn = get_display_names(session)
        ratings = _get_player_ratings(session, month)
        if not ratings:
            bot.reply_to(msg, 'Рейтинг пока пуст.')
            return
        lines = [f'Рейтинг игроков за {month}', '━' * 24]
        for i, (uname, rating, matches) in enumerate(ratings, 1):
            real = dn.get(uname)
            name = f'{real} (@{uname})' if real else f'@{uname}'
            lines.append(f'  {i}. {name} — {rating} очк. ({matches} матч.)')
        text = '\n'.join(lines)
    finally:
        session.close()
    _send_stats_image(msg, text, f'Рейтинг за {current_month()}')


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
        today_matches = session.query(Match).filter(Match.date == today()).all()
        if not today_matches:
            bot.reply_to(msg, 'Сегодня матчей ещё не было.')
            return
        dn = get_display_names(session)

        # Берём всех игроков из команд (до 9)
        teams = session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username != '__placeholder__'
        ).all()
        usernames = list(set(t.player_username for t in teams))[:9]
        options = []
        for u in usernames:
            real = dn.get(u)
            options.append(f'{real}' if real else f'@{u}')

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
        
@bot.message_handler(func=lambda m: m.from_user.id in user_states
                     and user_states[m.from_user.id].get('mode') in ('record', 'add_goals')
                     and m.text is not None
                     and not m.text.startswith('/'))

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
    teams = state.get('teams', {})

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

    # Шаг 3: ввод счёта → сразу записываем матч
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
        _finish_match(msg, state)
        del user_states[uid]

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

    # Шаг 5 (подшаг): ввод кол-ва ассистов текстом
    elif step == 5 and state.get('awaiting_assist_count_for'):
        username = state['awaiting_assist_count_for']
        if not text.isdigit() or int(text) < 1:
            bot.reply_to(msg, 'Введи число ассистов (1, 2, 3, ...).')
            return
        count = int(text)
        state['assists'].append({'username': username, 'count': count})
        del state['awaiting_assist_count_for']
        dn = state.get('display_names', {})
        real = dn.get(username)
        label = f'{real} (@{username})' if real else f'@{username}'
        bot.reply_to(msg, f'{label}: {count} асс.')
        _send_assists_keyboard(msg.chat.id, state, 'Кто ещё отдал ассист?')

# ─── Запуск ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('Бот запущен...')
    bot.polling(none_stop=True)
