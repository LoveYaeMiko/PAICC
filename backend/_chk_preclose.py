import glob
import json
import sqlite3

db = glob.glob(r"C:\Users\wyxwi\Desktop\PAICC\backend\data\*.db")[0]
con = sqlite3.connect(db)
try:
    rows = con.execute(
        "SELECT action, timestamp, result FROM operation_logs "
        "WHERE action LIKE 'quant_preclose%' ORDER BY timestamp DESC LIMIT 3"
    ).fetchall()
    for action, ts, result in rows:
        print("=" * 60)
        print(action, ts)
        print(result if result else "(no result)")
finally:
    con.close()
