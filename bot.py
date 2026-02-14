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
        "/stats_month_img — месяц картинкой\n\n"
        "Для всех:\n"
        "/stats — вся статистика\n"
        "/stats_day — статистика дня\n"
        "/stats_month — статистика месяца\n"
        "/rating — рейтинг игроков\n\n"
        "Рейтинг: 1-е место за день = 3 очка, "
        "2-е = 2, 3-е = 1, остальные = 0\n"
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
            stat = PlayerStat(username=tp.player_username, month=month)
            session.add(stat)
        stat.matches += 1


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

# ─── Close day ───────────────────────────────────────────────────────────────
def compute_day_ranking(session, d):
    matches = session.query(Match).filter(Match.date == d).all()
    if not matches: return {}

    pts = defaultdict(int)
    gf  = defaultdict(int)
    ga  = defaultdict(int)

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

        for uname, tnum in player_team.items():
            place = places.get(tnum, 99)
            add = 0
            if place == 1: add = 3
            elif place == 2: add = 2
            elif place == 3: add = 1

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

            stat.matches += 1
            stat.player_points += add
            updated += 1

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
    print("Бот запущен")
    bot.polling(none_stop=True)
