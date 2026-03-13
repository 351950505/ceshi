import sqlite3

DB_NAME = "bili_monitor.db"


def init_db():

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        oid TEXT PRIMARY KEY,
        bv TEXT,
        title TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        rpid TEXT PRIMARY KEY,
        oid TEXT
    )
    """)

    conn.commit()
    conn.close()


def clear_videos():

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM videos")

    conn.commit()
    conn.close()


def add_video_to_db(oid, bv, title):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO videos VALUES (?,?,?)",
        (oid, bv, title)
    )

    conn.commit()
    conn.close()


def get_monitored_videos():

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT oid,bv,title FROM videos")

    rows = cursor.fetchall()

    conn.close()

    return rows


def add_comment_to_db(rpid, oid):

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO comments VALUES (?,?)",
        (rpid, oid)
    )

    conn.commit()
    conn.close()