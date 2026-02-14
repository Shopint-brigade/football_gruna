# pip install pyTelegramBotAPI SQLAlchemy psycopg2-binary APScheduler pytz requests
import os
import datetime
import telebot
from telebot import types
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from collections import defaultdict

# ─── Настройки ───────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ['BOT_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']
CHAT_ID      = int(os.environ.get('CHAT_ID', '0'))
ADMIN_IDS    = [int(x) for x in os.environ.get('ADMIN_IDS', '123456789').split(',')]
TZ = pytz.timezone('Europe/Moscow')

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

bot = telebot.TeleBot(BOT_TOKEN)

# ─── База данных ─────────────────────────────────────────────────────────────
engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

class DailyVote(Base):
    __tablename__ = 'daily_votes'
    id          = Column(Integer, primary_key=True)
    date        = Column(Date, nullable=False)
    user_id     = Column(BigInteger, nullable=False)
    username    = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    choice      = Column(Integer, nullable=False)  # 0=абонемент, 2=разовая

class TeamToday(Base):
    __tablename__ = 'teams_today'
    id            = Column(Integer, primary_key=True)
    date          = Column(Date, nullable=False)
    team_number   = Column(Integer, nullable=False)
    team_name     = Column(String, nullable=False)
    player_username = Column(String, nullable=False)

class Match(Base):
    __tablename__ = 'matches'
    id          = Column(Integer, primary_key=True)
    date        = Column(Date, nullable=False)
    team_a_num  = Column(Integer, nullable=False)
    score_a     = Column(Integer, default=0)
    team_b_num  = Column(Integer, nullable=False)
    score_b     = Column(Integer, default=0)
    month       = Column(String, nullable=False)  # "2025-02"

class PlayerStat(Base):
    __tablename__ = 'player_stats'
    id            = Column(Integer, primary_key=True)
    username      = Column(String, nullable=False)
    month         = Column(String, nullable=False)
    matches       = Column(Integer, default=0)
    player_points = Column(Integer, default=0)     # 3/2/1 за места дня
    day_wins      = Column(Integer, default=0)     # ручная корректировка

Base.metadata.create_all(engine)

# ─── Вспомогательные функции ─────────────────────────────────────────────────
def today():
    return datetime.datetime.now(TZ).date()

def current_month():
    return today().strftime('%Y-%m')

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_today_players(session):
    return session.query(DailyVote).filter(
        DailyVote.date == today(),
        DailyVote.choice.in_([0, 2])
    ).all()

def get_today_teams(session):
    return session.query(TeamToday).filter(TeamToday.date == today()).all()

def get_team_names_today(session):
    teams = get_today_teams(session)
    return {t.team_number: t.team_name for t in teams}

def get_display_names(session):
    votes = session.query(DailyVote).filter(DailyVote.date == today()).all()
    return {v.username: v.display_name for v in votes if v.display_name}

# ─── Авто-poll по четвергам ──────────────────────────────────────────────────
def send_thursday_poll():
    if CHAT_ID == 0: return
    bot.send_poll(
        chat_id=CHAT_ID,
        question='Сегодня вечером играем?',
        options=['Играю по абонементу', 'Не играю сегодня', 'Хочу вписаться за разовую'],
        is_anonymous=False,
        allows_multiple_answers=False
    )

scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(send_thursday_poll, 'cron', day_of_week='thu', hour=10, minute=0)
scheduler.start()

# ─── Poll handler ────────────────────────────────────────────────────────────
@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    user = poll_answer.user
    uid = user.id
    uname = user.username or user.first_name or str(uid)
    display_name = ' '.join(filter(None, [user.first_name, user.last_name])).strip() or None
    choice = poll_answer.option_ids[0] if poll_answer.option_ids else None

    session = Session()
    try:
        # Удаляем старый голос за сегодня
        old = session.query(DailyVote).filter(
            DailyVote.date == today(), DailyVote.user_id == uid
        ).first()
        if old:
            session.delete(old)

        if choice is not None and choice != 1:  # не "не играю"
            session.add(DailyVote(
                date=today(), user_id=uid, username=uname,
                display_name=display_name, choice=choice
            ))
        session.commit()
    except Exception as e:
        print(f"Poll save error: {e}")
    finally:
        session.close()

# ─── Команды админа ──────────────────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
def cmd_help(msg):
    text = (
        "Команды админа:\n"
        "/players          — игроки на сегодня\n"
        "/add_player Имя    — добавить вручную\n"
        "/edit_player @user Имя\n"
        "/remove_player @user\n"
        "/clear_today       — очистить день\n"
        "/set_teams N       — 2–6 команд\n"
        "/set_team_name N Имя\n"
        "/add_to_team       — интерактивное добавление\n"
        "/teams             — показать составы\n"
        "/record            — записать результат матча (только счёт)\n"
        "/close_day         — закрыть день → начислить очки за места\n"
        "/adjust @user day_wins +1   — корректировка\n"
        "/auto_teams N      — авто-распределение по очкам\n"
        "/reset_month       — обнулить месяц\n"
        "/poll              — запустить опрос вручную\n\n"
        "Для всех:\n"
        "/stats\n"
        "/stats_day\n"
        "/stats_month\n"
        "/rating\n"
    )
    bot.reply_to(msg, text)

# ─── Игроки сегодня ──────────────────────────────────────────────────────────
@bot.message_handler(commands=['players'])
def cmd_players(msg):
    if not is_admin(msg.from_user.id): return
    session = Session()
    try:
        votes = get_today_players(session)
        if not votes:
            bot.reply_to(msg, "Сегодня никто не записался.")
            return
        lines = []
        dn = get_display_names(session)
        for v in votes:
            name = dn.get(v.username) or ''
            label = "абонемент" if v.choice == 0 else "разовая"
            lines.append(f"{name} @{v.username} ({label})")
        bot.reply_to(msg, f"Игроки сегодня ({len(lines)}):\n" + "\n".join(lines))
    finally:
        session.close()

# ─── Добавление / редактирование / удаление игроков ──────────────────────────
@bot.message_handler(commands=['add_player'])
def cmd_add_player(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "Формат: /add_player Имя Фамилия\nили /add_player @username Имя")
        return

    raw = parts[1].strip()
    tokens = raw.split()
    if tokens[0].startswith('@'):
        username = tokens[0][1:]
        display_name = " ".join(tokens[1:]) or None
    else:
        display_name = raw
        username = raw.replace(" ", "_").lower()

    session = Session()
    try:
        exists = session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.username == username
        ).first()
        if exists:
            bot.reply_to(msg, f"@{username} уже есть.")
            return
        session.add(DailyVote(
            date=today(), user_id=0, username=username,
            display_name=display_name, choice=0
        ))
        session.commit()
        label = f"{display_name} (@{username})" if display_name else f"@{username}"
        bot.reply_to(msg, f"Добавлен: {label}")
    finally:
        session.close()

@bot.message_handler(commands=['edit_player'])
def cmd_edit_player(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].startswith('@'):
        bot.reply_to(msg, "Формат: /edit_player @username Новое Имя")
        return
    username = parts[1][1:]
    new_name = parts[2].strip()

    session = Session()
    try:
        vote = session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.username == username
        ).first()
        if not vote:
            bot.reply_to(msg, f"@{username} не найден.")
            return
        vote.display_name = new_name
        session.commit()
        bot.reply_to(msg, f"@{username} → {new_name}")
    finally:
        session.close()

@bot.message_handler(commands=['remove_player'])
def cmd_remove_player(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].startswith('@'):
        bot.reply_to(msg, "Формат: /remove_player @username")
        return
    username = parts[1][1:]

    session = Session()
    try:
        vote = session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.username == username
        ).first()
        if not vote:
            bot.reply_to(msg, f"@{username} не найден.")
            return
        session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username == username
        ).delete()
        session.delete(vote)
        session.commit()
        bot.reply_to(msg, f"@{username} удалён.")
    finally:
        session.close()

@bot.message_handler(commands=['clear_today'])
def cmd_clear_today(msg):
    if not is_admin(msg.from_user.id): return
    session = Session()
    try:
        session.query(DailyVote).filter(DailyVote.date == today()).delete()
        session.query(TeamToday).filter(TeamToday.date == today()).delete()
        session.query(Match).filter(Match.date == today()).delete()
        session.commit()
        bot.reply_to(msg, "Данные дня очищены.")
    finally:
        session.close()

# ─── Команды и составы ───────────────────────────────────────────────────────
@bot.message_handler(commands=['set_teams'])
def cmd_set_teams(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(msg, "Формат: /set_teams 3–6")
        return
    n = int(parts[1])
    if n < 2 or n > 6:
        bot.reply_to(msg, "от 2 до 6 команд")
        return

    session = Session()
    try:
        session.query(TeamToday).filter(TeamToday.date == today()).delete()
        default_names = ['Красные','Зелёные','Синие','Жёлтые','Белые','Чёрные']
        for i in range(1, n+1):
            session.add(TeamToday(
                date=today(),
                team_number=i,
                team_name=default_names[i-1],
                player_username="__empty__"
            ))
        session.commit()
        bot.reply_to(msg, f"Создано {n} команд.")
    finally:
        session.close()

@bot.message_handler(commands=['set_team_name'])
def cmd_set_team_name(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.reply_to(msg, "Формат: /set_team_name 2 Синие")
        return
    num = int(parts[1])
    name = parts[2].strip()

    session = Session()
    try:
        teams = session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.team_number == num
        ).all()
        if not teams:
            bot.reply_to(msg, f"Команда {num} не найдена.")
            return
        for t in teams:
            t.team_name = name
        session.commit()
        bot.reply_to(msg, f"Команда {num} → {name}")
    finally:
        session.close()

# ─── Интерактивное добавление в команды ──────────────────────────────────────
user_states = {}

def _get_unassigned(session):
    players = get_today_players(session)
    assigned = {t.player_username for t in session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.player_username != "__empty__"
    )}
    return [p for p in players if p.username not in assigned]

def _team_summary(session):
    dn = get_display_names(session)
    teams = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.player_username != "__empty__"
    ).order_by(TeamToday.team_number).all()
    if not teams: return "Никто не распределён."
    grouped = defaultdict(list)
    names = {}
    for t in teams:
        grouped[t.team_number].append(t.player_username)
        names[t.team_number] = t.team_name
    lines = []
    for num in sorted(grouped):
        ps = [f"{dn.get(u) or ''} @{u}".strip() for u in grouped[num]]
        lines.append(f"#{num} {names.get(num,'')} : {', '.join(ps)}")
    return "\n".join(lines)

@bot.message_handler(commands=['add_to_team'])
def cmd_add_to_team(msg):
    if not is_admin(msg.from_user.id): return
    session = Session()
    try:
        if not get_team_names_today(session):
            bot.reply_to(msg, "Сначала /set_teams")
            return
        if not get_today_players(session):
            bot.reply_to(msg, "Никто не записался.")
            return
    finally:
        session.close()

    user_states[msg.from_user.id] = {'mode':'add_to_team'}
    send_player_choice(msg.chat.id, msg.from_user.id)

def send_player_choice(chat_id, uid):
    session = Session()
    try:
        un = _get_unassigned(session)
        if not un:
            summary = _team_summary(session)
            bot.send_message(chat_id, f"Все распределены!\n\n{summary}")
            user_states.pop(uid, None)
            return
        dn = get_display_names(session)
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in un:
            label = dn.get(p.username) or f"@{p.username}"
            markup.add(types.InlineKeyboardButton(label, callback_data=f"addp:{p.username}"))
        markup.add(types.InlineKeyboardButton("Готово", callback_data="add_done"))
        summary = _team_summary(session)
        bot.send_message(chat_id,
            f"Текущие составы:\n{summary}\n\nВыбери игрока:",
            reply_markup=markup
        )
    finally:
        session.close()

@bot.callback_query_handler(func=lambda c: c.data.startswith(('addp:','add_')))
def cb_add_to_team(c):
    uid = c.from_user.id
    if uid not in user_states or user_states[uid]['mode'] != 'add_to_team':
        bot.answer_callback_query(c.id, "Сессия завершена")
        return

    data = c.data
    if data == "add_done":
        bot.delete_message(c.message.chat.id, c.message.message_id)
        session = Session()
        try:
            bot.send_message(c.message.chat.id, _team_summary(session))
        finally:
            session.close()
        user_states.pop(uid, None)
        return

    if data.startswith("addp:"):
        username = data[5:]
        bot.delete_message(c.message.chat.id, c.message.message_id)
        session = Session()
        try:
            names = get_team_names_today(session)
            dn = get_display_names(session)
            label = dn.get(username) or f"@{username}"
        finally:
            session.close()

        markup = types.InlineKeyboardMarkup(row_width=2)
        for num, name in sorted(names.items()):
            markup.add(types.InlineKeyboardButton(f"{num}. {name}", callback_data=f"addt:{num}:{username}"))
        markup.add(types.InlineKeyboardButton("← Назад", callback_data="add_back"))

        bot.send_message(c.message.chat.id, f"Куда добавить {label}?", reply_markup=markup)

    elif data.startswith("addt:"):
        _, team_str, username = data.split(":", 2)
        team_num = int(team_str)
        bot.delete_message(c.message.chat.id, c.message.message_id)

        session = Session()
        try:
            name = session.query(TeamToday).filter(
                TeamToday.date == today(),
                TeamToday.team_number == team_num
            ).first().team_name
            session.query(TeamToday).filter(
                TeamToday.date == today(),
                TeamToday.player_username == username
            ).delete()
            session.add(TeamToday(
                date=today(), team_number=team_num,
                team_name=name, player_username=username
            ))
            session.commit()
            dn = get_display_names(session)
            label = dn.get(username) or f"@{username}"
            bot.send_message(c.message.chat.id, f"{label} → команда {team_num}")
            send_player_choice(c.message.chat.id, uid)
        finally:
            session.close()

    elif data == "add_back":
        bot.delete_message(c.message.chat.id, c.message.message_id)
        send_player_choice(c.message.chat.id, uid)

@bot.message_handler(commands=['teams'])
def cmd_teams(msg):
    session = Session()
    try:
        text = _team_summary(session)
        bot.reply_to(msg, text or "Составов пока нет.")
    finally:
        session.close()

# ─── Запись результата матча (только счёт) ───────────────────────────────────
@bot.message_handler(commands=['record'])
def cmd_record(msg):
    if not is_admin(msg.from_user.id): return
    session = Session()
    try:
        names = get_team_names_today(session)
        if not names:
            bot.reply_to(msg, "Сначала создайте команды: /set_teams")
            return
    finally:
        session.close()

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for num in sorted(names):
        markup.add(f"{num}. {names[num]}")

    user_states[msg.from_user.id] = {'step':1, 'teams':names}
    bot.reply_to(msg, "Выберите команду А:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.from_user.id in user_states and user_states[m.from_user.id].get('step'))
def handle_record(msg):
    uid = msg.from_user.id
    state = user_states[uid]
    text = msg.text.strip()

    if text.lower() in ('отмена','/cancel'):
        del user_states[uid]
        bot.reply_to(msg, "Отменено.", reply_markup=types.ReplyKeyboardRemove())
        return

    step = state['step']
    teams = state['teams']

    if step == 1:
        for k,v in teams.items():
            if text.startswith(str(k)):
                state['team_a'] = k
                state['step'] = 2
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for n in teams:
                    if n != k:
                        markup.add(f"{n}. {teams[n]}")
                bot.reply_to(msg, "Выберите команду B:", reply_markup=markup)
                return
        bot.reply_to(msg, "Выберите из списка.")

    elif step == 2:
        for k,v in teams.items():
            if text.startswith(str(k)) and k != state['team_a']:
                state['team_b'] = k
                state['step'] = 3
                bot.reply_to(msg, "Введите счёт (пример: 4:2):", reply_markup=types.ReplyKeyboardRemove())
                return
        bot.reply_to(msg, "Выберите другую команду.")

    elif step == 3:
        if ':' not in text:
            bot.reply_to(msg, "Формат: 4:2")
            return
        a, b = text.split(':',1)
        try:
            sa = int(a.strip())
            sb = int(b.strip())
        except:
            bot.reply_to(msg, "Неверный формат.")
            return

        session = Session()
        try:
            session.add(Match(
                date=today(),
                team_a_num=state['team_a'],
                score_a=sa,
                team_b_num=state['team_b'],
                score_b=sb,
                month=current_month()
            ))
            session.commit()
            na = teams.get(state['team_a'], f"#{state['team_a']}")
            nb = teams.get(state['team_b'], f"#{state['team_b']}")
            bot.reply_to(msg, f"Записан матч: {na} {sa}:{sb} {nb}")
        finally:
            session.close()
            del user_states[uid]

# ─── Закрытие дня и начисление очков за места ───────────────────────────────
def get_day_ranking(session, date):
    matches = session.query(Match).filter(Match.date == date).all()
    if not matches:
        return {}

    points = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)

    for m in matches:
        a, b = m.team_a_num, m.team_b_num
        sa, sb = m.score_a, m.score_b
        gf[a] += sa; ga[a] += sb
        gf[b] += sb; ga[b] += sa
        if sa > sb:     points[a] += 3
        elif sb > sa:   points[b] += 3
        else:
            points[a] += 1
            points[b] += 1

    ranking = sorted(
        points.keys(),
        key=lambda t: (points[t], gf[t], -ga[t]),
        reverse=True
    )

    places = {}
    place = 1
    prev = None
    for i, t in enumerate(ranking):
        curr = (points[t], gf[t], -ga[t])
        if i > 0 and curr != prev:
            place = i + 1
        places[t] = place
        prev = curr

    return places

@bot.message_handler(commands=['close_day'])
def cmd_close_day(msg):
    if not is_admin(msg.from_user.id): return
    session = Session()
    try:
        matches = session.query(Match).filter(Match.date == today()).all()
        if not matches:
            bot.reply_to(msg, "Сегодня матчей нет.")
            return

        places = get_day_ranking(session, today())
        if not places:
            bot.reply_to(msg, "Не удалось посчитать места.")
            return

        players = session.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username != "__empty__"
        ).all()

        player_team = {p.player_username: p.team_number for p in players}
        month = current_month()
        updated = 0

        for username, tnum in player_team.items():
            place = places.get(tnum, 99)
            pts = 0
            if place == 1: pts = 3
            elif place == 2: pts = 2
            elif place == 3: pts = 1

            stat = session.query(PlayerStat).filter_by(username=username, month=month).first()
            if not stat:
                stat = PlayerStat(username=username, month=month)
                session.add(stat)

            stat.matches += 1
            stat.player_points += pts
            updated += 1

        session.commit()

        lines = ["День закрыт. Начислены очки:"]
        for pl, pt in [(1,3),(2,2),(3,1)]:
            ts = [str(t) for t,p in places.items() if p==pl]
            if ts:
                lines.append(f"{pl} место → +{pt} → команды {', '.join(ts)}")

        lines.append(f"\nОбновлено игроков: {updated}")
        bot.reply_to(msg, "\n".join(lines))
    finally:
        session.close()

# ─── Корректировка статистики ────────────────────────────────────────────────
@bot.message_handler(commands=['adjust'])
def cmd_adjust(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 4 or not parts[1].startswith('@'):
        bot.reply_to(msg, "Формат: /adjust @username day_wins +3")
        return

    username = parts[1][1:]
    field = parts[2].lower()
    val_str = parts[3]

    if field != 'day_wins':
        bot.reply_to(msg, "Пока можно корректировать только day_wins")
        return

    session = Session()
    try:
        month = current_month()
        stat = session.query(PlayerStat).filter_by(username=username, month=month).first()
        if not stat:
            stat = PlayerStat(username=username, month=month)
            session.add(stat)

        old = stat.day_wins
        try:
            if val_str.startswith('+'):
                stat.day_wins += int(val_str[1:])
            elif val_str.startswith('-'):
                stat.day_wins = max(0, stat.day_wins - int(val_str[1:]))
            else:
                stat.day_wins = max(0, int(val_str))
        except ValueError:
            bot.reply_to(msg, "Неверное число")
            return

        session.commit()
        bot.reply_to(msg, f"@{username} day_wins: {old} → {stat.day_wins}")
    finally:
        session.close()

# ─── Авто-распределение по текущим очкам ─────────────────────────────────────
@bot.message_handler(commands=['auto_teams'])
def cmd_auto_teams(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(msg, "/auto_teams 3–6")
        return
    n = int(parts[1])
    if n < 2 or n > 6:
        bot.reply_to(msg, "2–6")
        return

    session = Session()
    try:
        players = get_today_players(session)
        if len(players) < n:
            bot.reply_to(msg, f"Игроков {len(players)} < команд {n}")
            return

        month = current_month()
        stats = session.query(PlayerStat).filter_by(month=month).all()
        pts_map = {s.username: s.player_points + s.day_wins for s in stats}

        usernames = [p.username for p in players]
        sorted_usernames = sorted(
            usernames,
            key=lambda u: pts_map.get(u, 0),
            reverse=True
        )

        teams = [[] for _ in range(n)]
        for i, u in enumerate(sorted_usernames):
            teams[i % n].append(u)

        # Очистка и запись
        session.query(TeamToday).filter(TeamToday.date == today()).delete()
        names = get_team_names_today(session) or {i+1:f"Команда {i+1}" for i in range(n)}

        for i, group in enumerate(teams, 1):
            tname = names.get(i, f"Команда {i}")
            for u in group:
                session.add(TeamToday(
                    date=today(), team_number=i, team_name=tname, player_username=u
                ))
            # пустышка
            session.add(TeamToday(
                date=today(), team_number=i, team_name=tname, player_username="__empty__"
            ))

        session.commit()

        dn = get_display_names(session)
        lines = [f"Авто-распределение ({n} команд):"]
        for i, group in enumerate(teams, 1):
            ps = [f"{dn.get(u) or ''} @{u} ({pts_map.get(u,0)})".strip() for u in group]
            lines.append(f"#{i} {names.get(i,'')} : {', '.join(ps)}")
        bot.reply_to(msg, "\n".join(lines))
    finally:
        session.close()

# ─── Статистика ──────────────────────────────────────────────────────────────
def build_stats_day(session):
    matches = session.query(Match).filter(Match.date == today()).all()
    if not matches:
        return None

    names = get_team_names_today(session)
    points = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)

    for m in matches:
        a,b = m.team_a_num, m.team_b_num
        sa,sb = m.score_a, m.score_b
        gf[a] += sa; ga[a] += sb
        gf[b] += sb; ga[b] += sa
        if sa > sb:     points[a] += 3
        elif sb > sa:   points[b] += 3
        else:
            points[a] += 1
            points[b] += 1

    ranking = sorted(
        points.keys(),
        key=lambda t: (points[t], gf[t], -ga[t]),
        reverse=True
    )

    lines = [f"День {today()}", "─"*30]
    lines.append("Таблица дня:")
    for t in ranking:
        lines.append(f"  {names.get(t,f'#{t}')} : {points[t]} очк  |  {gf[t]}:{ga[t]}")

    lines.append("\nСоставы:")
    teams = session.query(TeamToday).filter(
        TeamToday.date == today(),
        TeamToday.player_username != "__empty__"
    ).order_by(TeamToday.team_number).all()
    gr = defaultdict(list)
    for t in teams:
        gr[t.team_number].append(t.player_username)
    dn = get_display_names(session)
    for num in sorted(gr):
        ps = [f"{dn.get(u) or ''} @{u}".strip() for u in gr[num]]
        lines.append(f"  {names.get(num,f'#{num}')} : {', '.join(ps)}")

    return "\n".join(lines)

def build_stats_month(session):
    month = current_month()
    teams = session.query(Match).filter(Match.month == month).all()
    if not teams:
        return None

    points = defaultdict(int)
    gf = defaultdict(int)
    ga = defaultdict(int)
    team_names = {}

    for m in teams:
        a,b = m.team_a_num, m.team_b_num
        sa,sb = m.score_a, m.score_b
        key_a = f"{a}_{m.date}"
        key_b = f"{b}_{m.date}"
        team_names.setdefault(key_a, f"#{a} {m.date}")
        team_names.setdefault(key_b, f"#{b} {m.date}")
        gf[key_a] += sa; ga[key_a] += sb
        gf[key_b] += sb; ga[key_b] += sa
        if sa > sb:     points[key_a] += 3
        elif sb > sa:   points[key_b] += 3
        else:
            points[key_a] += 1
            points[key_b] += 1

    ranking = sorted(
        points.keys(),
        key=lambda k: (points[k], gf[k], -ga[k]),
        reverse=True
    )

    lines = [f"Месяц {month}", "─"*30, "Команды:"]
    seen = set()
    for k in ranking:
        if k in seen: continue
        seen.add(k)
        lines.append(f"  {team_names[k]} : {points[k]} очк  |  {gf[k]}:{ga[k]}")

    return "\n".join(lines)

def build_rating(session):
    month = current_month()
    stats = session.query(PlayerStat).filter_by(month=month).all()
    if not stats:
        return None

    dn = get_display_names(session)
    items = []
    for s in stats:
        total = s.player_points + s.day_wins
        name = dn.get(s.username) or f"@{s.username}"
        items.append((total, name, s.player_points, s.day_wins, s.matches))

    items.sort(reverse=True)
    lines = [f"Рейтинг {month}", "─"*30]
    for i, (tot, name, pts, dw, m) in enumerate(items, 1):
        lines.append(f"{i}. {name}  —  {tot}  ({pts} + {dw} дн.поб.)  |  {m} игр")
    return "\n".join(lines)

@bot.message_handler(commands=['stats', 'stats_day', 'stats_month', 'rating'])
def cmd_stats(msg):
    cmd = msg.text.split()[0][1:]
    session = Session()
    try:
        if cmd == 'stats':
            day = build_stats_day(session)
            mon = build_stats_month(session)
            rat = build_rating(session)
            parts = [p for p in [day, mon, rat] if p]
            text = "\n\n".join(parts) or "Нет данных"
        elif cmd == 'stats_day':
            text = build_stats_day(session) or "Сегодня матчей нет"
        elif cmd == 'stats_month':
            text = build_stats_month(session) or "Нет матчей за месяц"
        elif cmd == 'rating':
            text = build_rating(session) or "Рейтинг пуст"
        bot.reply_to(msg, text)
    finally:
        session.close()

# ─── Запуск ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Бот запущен")
    bot.polling(none_stop=True)
