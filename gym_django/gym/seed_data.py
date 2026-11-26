"""Demo seed data (ported unchanged from the Flask app.py, constants only)."""
# ===== ADDED (Iskander): SEED DATA ======================================
# CORE seed: small and fixed. It is loaded into gym.db AND into every in-memory test database,
# so the 53 team tests keep working. To plug in the real team data, edit these lists.
SEED_MEMBERS = [  # (name, email, membership_status, role, balance); password "1234" (DEMO_PW_HASH)
    ("Aldair", "aldair@kbtu.kz", "active", "admin", 85.0),
    ("Iskander", "iskander@kbtu.kz", "active", "member", 85.0),
    ("Adil", "adil@kbtu.kz", "active", "member", 100.0),
    ("Zhanibek", "zhanibek@kbtu.kz", "active", "member", 100.0),
    ("Expired Member", "expired@kbtu.kz", "expired", "member", 100.0),
]
SEED_SESSIONS = [  # (title, trainer, days_from_today, start_time, capacity, price)
    ("Yoga", "Aigerim", 1, "10:00", 2, 15.0),
    ("Boxing", "Daniyar", 2, "18:00", 3, 20.0),
    ("Fitness", "Madina", 3, "09:00", 4, 12.0),
    ("Yoga (past)", "Aigerim", -1, "10:00", 5, 15.0),
]
SEED_BOOKINGS = [(1, 1), (2, 1)]  # (member_id, session_id): Yoga is full; every booking also gets a paid Payment

# EXTENDED demo data: only for the real gym.db (python app.py), NOT for the tests.
# A realistic two-week timetable with bigger halls, 30 more members and their bookings,
# so the occupancy chart looks like a real club and 2 booked places are no longer "100%".
EXT_MEMBER_NAMES = [
    "Alua", "Bekzat", "Dana", "Yerlan", "Gulnara", "Timur", "Madi", "Aruzhan", "Nurlan", "Saltanat",
    "Dias", "Kamila", "Rustem", "Zarina", "Arman", "Ayana", "Miras", "Lyazzat", "Askar", "Dinara",
    "Olzhas", "Aigul", "Samat", "Nazerke", "Azamat", "Karina", "Ilyas", "Assel", "Ruslan", "Medina",
]
EXT_CAPACITY_UPGRADE = {"Yoga": 20, "Boxing": 25, "Fitness": 30, "Yoga (past)": 20}  # the 4 core sessions get real halls
# (title, trainer, weekdays 0=Mon..6=Sun, start_time, capacity, price, typical fill 0..1)
EXT_SCHEDULE = [
    ("Crossfit", "Daniyar", (0, 1, 3, 4), "07:00", 16, 15.0, 0.60),
    ("Morning Yoga", "Aigerim", (0, 2, 4), "08:00", 20, 12.0, 0.55),
    ("Weekend Yoga", "Aigerim", (5, 6), "09:00", 25, 12.0, 0.60),
    ("Stretch & Mobility", "Dana", (1, 3), "09:00", 18, 10.0, 0.50),
    ("Pilates", "Madina", (1, 3, 5), "10:00", 18, 14.0, 0.55),
    ("Weekend Fitness", "Madina", (5, 6), "10:00", 30, 12.0, 0.75),
    ("Core Training", "Sanzhar", (0, 2, 4), "11:00", 18, 13.0, 0.50),
    ("Weekend HIIT", "Daniyar", (5, 6), "11:00", 25, 13.0, 0.70),
    ("Lunch HIIT", "Daniyar", (0, 1, 2, 3, 4), "12:00", 20, 13.0, 0.50),
    ("Family Fitness", "Madina", (5, 6), "12:00", 25, 11.0, 0.55),
    ("Express Fitness", "Dana", (1, 2, 3), "13:00", 20, 11.0, 0.45),
    ("Open Boxing", "Daniyar", (5,), "14:00", 20, 15.0, 0.50),
    ("Strength Basics", "Sanzhar", (0, 2, 4), "15:00", 18, 14.0, 0.45),
    ("Dance Cardio", "Madina", (5, 6), "15:00", 25, 12.0, 0.50),
    ("Strength Club", "Sanzhar", (1, 3, 6), "16:00", 18, 14.0, 0.55),
    ("Weekend Spinning", "Dana", (5, 6), "16:00", 25, 14.0, 0.60),
    ("Teen Fitness", "Madina", (0, 1, 2, 3, 4), "17:00", 20, 10.0, 0.60),
    ("Spinning", "Dana", (0, 1, 2, 3, 4), "18:00", 25, 14.0, 0.85),
    ("Evening Crossfit", "Sanzhar", (0, 2, 4), "18:00", 18, 15.0, 0.80),
    ("Boxing Group", "Daniyar", (0, 2, 4), "19:00", 25, 20.0, 0.85),
    ("Zumba", "Madina", (1, 3), "19:00", 30, 14.0, 0.90),
    ("Functional Training", "Sanzhar", (0, 1, 2, 3, 4), "19:00", 22, 16.0, 0.80),
    ("Evening Yoga", "Aigerim", (0, 1, 2, 3, 6), "20:00", 25, 12.0, 0.65),
    ("Late HIIT", "Daniyar", (1, 3, 4), "20:00", 20, 13.0, 0.70),
    ("Weekend Zumba", "Madina", (5, 6), "18:00", 30, 14.0, 0.70),
    ("Weekend Boxing", "Daniyar", (5, 6), "19:00", 20, 18.0, 0.70),
    ("Stretching", "Aigerim", (0, 1, 2, 3, 4, 5), "21:00", 20, 10.0, 0.35),
]
EXT_DAYS_BACK, EXT_DAYS_AHEAD = 10, 3  # 14 days: every weekday appears twice; the page lists the next 4 days
EXT_START_BALANCE = 800.0  # money of every demo member before the demo bookings are paid
