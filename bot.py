# pip install pyTelegramBotAPI SQLAlchemy psycopg2-binary APScheduler pytz
import os
import datetime
import telebot
from telebot import types
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from collections import defaultdict

# ─── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ['BOT_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']
CHAT_ID      = int(os.environ.get('CHAT_ID', '0'))
ADMIN_IDS    = [int(x) for x in os.environ.get('ADMIN_IDS', '123456789').split(',')]
TZ = pytz.timezone('Europe/Moscow')

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

bot = telebot.TeleBot(BOT_TOKEN)

# ─── Database ────────────────────────────────────────────────────────────────
engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

class DailyVote(Base):
    __tablename__ = 'daily_votes'
    id           = Column(Integer, primary_key=True)
    date         = Column(Date, nullable=False)
    user_id      = Column(BigInteger, nullable=False)
    username     = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    choice       = Column(Integer, nullable=False)  # 0=season, 2=one-time

class TeamToday(Base):
    __tablename__ = 'teams_today'
    id             = Column(Integer, primary_key=True)
    date           = Column(Date, nullable=False)
    team_number    = Column(Integer, nullable=False)
    team_name      = Column(String, nullable=False)
    player_username = Column(String, nullable=False)

class Match(Base):
    __tablename__ = 'matches'
    id          = Column(Integer, primary_key=True)
    date        = Column(Date, nullable=False)
    team_a_num  = Column(Integer, nullable=False)
    score_a     = Column(Integer, default=0)
    team_b_num  = Column(Integer, nullable=False)
    score_b     = Column(Integer, default=0)
    team_a_name = Column(String, nullable=True)   # snapshot at record time
    team_b_name = Column(String, nullable=True)
    month       = Column(String, nullable=False)

class PlayerStat(Base):
    __tablename__ = 'player_stats'
    id            = Column(Integer, primary_key=True)
    username      = Column(String, nullable=False)
    month         = Column(String, nullable=False)
    matches       = Column(Integer, default=0)
    player_points = Column(Integer, default=0)    # only 3/2/1/0 from daily places

Base.metadata.create_all(engine)

# ─── Helpers ─────────────────────────────────────────────────────────────────
def today():
    return datetime.datetime.now(TZ).date()

def current_month():
    return today().strftime('%Y-%m')

def is_admin(uid):
    return uid in ADMIN_IDS

def get_today_players(session):
    return session.query(DailyVote).filter(
        DailyVote.date == today(),
        DailyVote.choice.in_([0, 2])
    ).all()

def get_team_names_today(session):
    teams = session.query(TeamToday).filter(TeamToday.date == today()).all()
    return {t.team_number: t.team_name for t in teams}

def get_display_names(session):
    votes = session.query(DailyVote).filter(DailyVote.date == today()).all()
    return {v.username: v.display_name for v in votes if v.display_name}

# ─── Thursday poll ───────────────────────────────────────────────────────────
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

@bot.poll_answer_handler()
def on_poll_answer(poll_answer):
    user = poll_answer.user
    uid = user.id
    uname = user.username or user.first_name or str(uid)
    dname = ' '.join(filter(None, [user.first_name, user.last_name])).strip() or None
    choice = poll_answer.option_ids[0] if poll_answer.option_ids else None

    session = Session()
    try:
        session.query(DailyVote).filter(
            DailyVote.date == today(),
            DailyVote.user_id == uid
        ).delete(synchronize_session=False)

        if choice is not None and choice != 1:
            session.add(DailyVote(
                date=today(), user_id=uid, username=uname,
                display_name=dname, choice=choice
            ))
        session.commit()
    except Exception as e:
        print(f"Poll error: {e}")
    finally:
        session.close()

# ─── Help ────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['start', 'help'])
def cmd_help(msg):
    text = (
        "Команды админа:\n"
        "/players          — кто сегодня\n"
        "/add_player Имя    — добавить вручную\n"
        "/edit_player @user Имя\n"
        "/remove_player @user\n"
        "/clear_today       — очистить день\n"
        "/set_teams N       — 2–6 команд\n"
        "/set_team_name N Имя\n"
        "/add_to_team       — распределение\n"
        "/teams             — текущие составы\n"
        "/record            — записать матч (счёт)\n"
        "/close_day         — закрыть день → начислить 3/2/1/0\n"
        "/adjust @user points +5    — корректировка очков\n"
        "/auto_teams N      — авто по очкам\n"
        "/reset_month       — обнулить месяц\n"
        "/poll              — опрос вручную\n\n"
        "Для всех:\n"
        "/stats       — день + месяц + рейтинг\n"
        "/stats_day   — таблица дня + составы\n"
        "/stats_month — таблица месяца\n"
        "/rating      — рейтинг игроков (очки + матчи)\n"
    )
    bot.reply_to(msg, text)

# ─── Record match ────────────────────────────────────────────────────────────
user_states = {}

@bot.message_handler(commands=['record'])
def cmd_record(msg):
    if not is_admin(msg.from_user.id): return
    with Session() as s:
        names = get_team_names_today(s)
        if not names:
            bot.reply_to(msg, "Сначала /set_teams")
            return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for n in sorted(names):
        markup.add(f"{n}. {names[n]}")

    user_states[msg.from_user.id] = {'step':1, 'teams':names}
    bot.reply_to(msg, "Команда А:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.from_user.id in user_states)
def handle_record_step(msg):
    uid = msg.from_user.id
    if uid not in user_states: return
    state = user_states[uid]
    text = msg.text.strip()

    if text.lower() in ('/cancel', 'отмена'):
        del user_states[uid]
        bot.reply_to(msg, "Отменено.", reply_markup=types.ReplyKeyboardRemove())
        return

    step = state['step']
    teams = state['teams']

    if step == 1:
        for k in teams:
            if text.startswith(str(k)):
                state['a_num'] = k
                state['a_name'] = teams[k]
                state['step'] = 2
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for n in teams:
                    if n != k: markup.add(f"{n}. {teams[n]}")
                bot.reply_to(msg, "Команда B:", reply_markup=markup)
                return
        bot.reply_to(msg, "Выберите из списка.")

    elif step == 2:
        for k in teams:
            if text.startswith(str(k)) and k != state['a_num']:
                state['b_num'] = k
                state['b_name'] = teams[k]
                state['step'] = 3
                bot.reply_to(msg, "Счёт (пример 5:3):", reply_markup=types.ReplyKeyboardRemove())
                return
        bot.reply_to(msg, "Выберите другую команду.")

    elif step == 3:
        if ':' not in text:
            bot.reply_to(msg, "Ожидается формат 5:3")
            return
        try:
            sa, sb = map(int, [x.strip() for x in text.split(':',1)])
        except:
            bot.reply_to(msg, "Неверный формат")
            return

        with Session() as s:
            s.add(Match(
                date=today(),
                team_a_num=state['a_num'],
                team_a_name=state['a_name'],
                score_a=sa,
                team_b_num=state['b_num'],
                team_b_name=state['b_name'],
                score_b=sb,
                month=current_month()
            ))
            s.commit()

        bot.reply_to(msg,
            f"Матч: {state['a_name']} {sa} : {sb} {state['b_name']}",
            reply_markup=types.ReplyKeyboardRemove()
        )
        del user_states[uid]

# ─── Close day ───────────────────────────────────────────────────────────────
def compute_day_ranking(session, d):
    matches = session.query(Match).filter(Match.date == d).all()
    if not matches: return {}

    pts = defaultdict(int)
    gf  = defaultdict(int)
    ga  = defaultdict(int)

    for m in matches:
        a, b = m.team_a_num, m.team_b_num
        sa, sb = m.score_a, m.score_b
        gf[a] += sa; ga[a] += sb
        gf[b] += sb; ga[b] += sa
        if sa > sb:     pts[a] += 3
        elif sb > sa:   pts[b] += 3
        else:
            pts[a] += 1
            pts[b] += 1

    ranked = sorted(
        pts.keys(),
        key=lambda t: (pts[t], gf[t], -ga[t]),
        reverse=True
    )

    places = {}
    place = 1
    prev = None
    for i, t in enumerate(ranked):
        curr = (pts[t], gf[t], -ga[t])
        if i > 0 and curr != prev:
            place = i + 1
        places[t] = place
        prev = curr

    return places

@bot.message_handler(commands=['close_day'])
def cmd_close_day(msg):
    if not is_admin(msg.from_user.id): return
    with Session() as s:
        if not s.query(Match).filter(Match.date == today()).first():
            bot.reply_to(msg, "Сегодня матчей не было.")
            return

        places = compute_day_ranking(s, today())
        players = s.query(TeamToday).filter(
            TeamToday.date == today(),
            TeamToday.player_username != "__empty__"
        ).all()

        player_team = {p.player_username: p.team_number for p in players}
        month = current_month()
        updated = 0

        for uname, tnum in player_team.items():
            place = places.get(tnum, 99)
            add = 0
            if place == 1: add = 3
            elif place == 2: add = 2
            elif place == 3: add = 1

            stat = s.query(PlayerStat).filter_by(username=uname, month=month).first()
            if not stat:
                stat = PlayerStat(username=uname, month=month)
                s.add(stat)

            stat.matches += 1
            stat.player_points += add
            updated += 1

        s.commit()

        lines = ["День закрыт. Начислено:"]
        for pl, val in [(1,3), (2,2), (3,1)]:
            ts = [str(t) for t, p in places.items() if p == pl]
            if ts:
                lines.append(f"{pl} место → +{val} → команды {', '.join(ts)}")

        lines.append(f"\nОбновлено игроков: {updated}")
        bot.reply_to(msg, "\n".join(lines))

# ─── Adjust points only ──────────────────────────────────────────────────────
@bot.message_handler(commands=['adjust'])
def cmd_adjust(msg):
    if not is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 4 or not parts[1].startswith('@'):
        bot.reply_to(msg, "Формат: /adjust @username points +5\nили /adjust @username points -3\nили /adjust @username points 12")
        return

    username = parts[1][1:]
    field = parts[2].lower()
    val_str = parts[3]

    if field != 'points':
        bot.reply_to(msg, "Можно корректировать только поле points")
        return

    with Session() as s:
        month = current_month()
        stat = s.query(PlayerStat).filter_by(username=username, month=month).first()
        if not stat:
            stat = PlayerStat(username=username, month=month)
            s.add(stat)

        old = stat.player_points

        try:
            if val_str.startswith('+'):
                stat.player_points += int(val_str[1:])
            elif val_str.startswith('-'):
                stat.player_points = max(0, stat.player_points - int(val_str[1:]))
            else:
                stat.player_points = max(0, int(val_str))
        except ValueError:
            bot.reply_to(msg, "Неверное значение")
            return

        s.commit()
        bot.reply_to(msg, f"@{username} points: {old} → {stat.player_points} ({month})")

# ─── Stats & Rating ──────────────────────────────────────────────────────────
def build_day_table(session):
    matches = session.query(Match).filter(Match.date == today()).all()
    if not matches: return None

    pts = defaultdict(int)
    gf  = defaultdict(int)
    ga  = defaultdict(int)
    names = {}

    for m in matches:
        a, b = m.team_a_num, m.team_b_num
        na = m.team_a_name or f"#{a}"
        nb = m.team_b_name or f"#{b}"
        names[a] = na
        names[b] = nb
        sa, sb = m.score_a, m.score_b
        gf[a] += sa; ga[a] += sb
        gf[b] += sb; ga[b] += sa
        if sa > sb:     pts[a] += 3
        elif sb > sa:   pts[b] += 3
        else:
            pts[a] += 1
            pts[b] += 1

    ranked = sorted(
        pts.keys(),
        key=lambda t: (pts[t], gf[t], -ga[t]),
        reverse=True
    )

    lines = [f"Таблица дня {today()}", "─"*40]
    for t in ranked:
        lines.append(f"{names[t]:<20} {pts[t]:2} очк   {gf[t]:2}:{ga[t]:2}")

    return "\n".join(lines)

def build_month_table(session):
    month = current_month()
    matches = session.query(Match).filter(Match.month == month).all()
    if not matches: return None

    pts = defaultdict(int)
    gf  = defaultdict(int)
    ga  = defaultdict(int)
    names = {}

    for m in matches:
        a, b = m.team_a_num, m.team_b_num
        na = m.team_a_name or f"#{a}"
        nb = m.team_b_name or f"#{b}"
        key_a = f"{a}_{m.date}"
        key_b = f"{b}_{m.date}"
        names[key_a] = na
        names[key_b] = nb
        sa, sb = m.score_a, m.score_b
        gf[key_a] += sa; ga[key_a] += sb
        gf[key_b] += sb; ga[key_b] += sa
        if sa > sb:     pts[key_a] += 3
        elif sb > sa:   pts[key_b] += 3
        else:
            pts[key_a] += 1
            pts[key_b] += 1

    ranked = sorted(
        pts.keys(),
        key=lambda k: (pts[k], gf[k], -ga[k]),
        reverse=True
    )

    lines = [f"Таблица месяца {month}", "─"*40]
    seen = set()
    for k in ranked:
        if k in seen: continue
        seen.add(k)
        lines.append(f"{names[k]:<20} {pts[k]:2} очк   {gf[k]:2}:{ga[k]:2}")

    return "\n".join(lines)

def build_rating_text(session):
    month = current_month()
    stats = session.query(PlayerStat).filter_by(month=month).all()
    if not stats: return None

    dn = get_display_names(session)
    rows = []
    for s in stats:
        name = dn.get(s.username, f"@{s.username}")
        rows.append((s.player_points, name, s.matches))

    rows.sort(reverse=True)
    lines = [f"Рейтинг — {month}", "─"*40]
    for i, (pts, name, m) in enumerate(rows, 1):
        line = f"{i:2}. {name:<22} {pts:3} очк"
        if m:
            line += f"  ({m} игр)"
        lines.append(line)

    return "\n".join(lines)

@bot.message_handler(commands=['stats', 'stats_day', 'stats_month', 'rating'])
def cmd_stats(msg):
    cmd = msg.text.lstrip('/').split()[0]
    with Session() as s:
        if cmd == 'stats':
            day   = build_day_table(s)
            mon   = build_month_table(s)
            rat   = build_rating_text(s)
            parts = [p for p in [day, mon, rat] if p]
            text = "\n\n".join(parts) or "Нет данных"
        elif cmd == 'stats_day':
            text = build_day_table(s) or "Сегодня матчей нет"
            # Можно добавить составы, если хотите — как в предыдущих версиях
        elif cmd == 'stats_month':
            text = build_month_table(s) or "Нет матчей за месяц"
        elif cmd == 'rating':
            text = build_rating_text(s) or "Рейтинг пуст"

        bot.reply_to(msg, text)

# ─── Запуск ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Бот запущен")
    bot.polling(none_stop=True)
