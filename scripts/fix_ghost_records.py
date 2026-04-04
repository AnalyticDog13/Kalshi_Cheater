"""One-time script to mark unfilled ghost DB records as cancelled."""
import sqlite3

conn = sqlite3.connect("data/trades.db")
c = conn.cursor()

c.execute("SELECT COUNT(*) FROM trades WHERE order_filled_time IS NULL AND order_cancelled = 0 AND strategy_arm != 'exploratory'")
count = c.fetchone()[0]
print(f"Pending ghost records to cancel: {count}")

c.execute("UPDATE trades SET order_cancelled = 1 WHERE order_filled_time IS NULL AND order_cancelled = 0 AND strategy_arm != 'exploratory'")
conn.commit()
print(f"Done. {c.rowcount} records marked as cancelled.")
conn.close()
