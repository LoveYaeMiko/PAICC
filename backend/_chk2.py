import glob
import sqlite3
import time

db = glob.glob(r"C:\Users\wyxwi\Desktop\PAICC\backend\data\*.db")[0]
con = sqlite3.connect(db)
try:
    rows = con.execute(
        "SELECT action, timestamp, result FROM operation_logs "
        "WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 8",
        (time.time() - 3600,),
    ).fetchall()
    for action, ts, result in rows:
        t = time.strftime("%H:%M:%S", time.localtime(ts))
        r = (result or "")[:160].replace("\n", " ")
        print(f"{t} | {action} | {r}")
finally:
    con.close()
