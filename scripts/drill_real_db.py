"""真库副本演练：对真实 history.db 副本跑 _copy_history_rows + _remap_scroll_index。
不动真库，验证真实 schema 与数据量下的行为。"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\Data\github_repo\qwenpaw-plugin-session-tools")
from session_ops import _copy_history_rows, _remap_scroll_index

REAL_DB = Path(r"C:\Users\TWD\.qwenpaw\workspaces\default\history.db")
PARENT = "1792351730741-iz0fipe"   # 库里最大的会话（1840 行）
FORK = "fork-drill-test.000"

tmp = Path(tempfile.mkdtemp())
copy_db = tmp / "history.db"
# 一致性快照：用 sqlite3 backup API（WAL 帧会被合并进快照），
# 直接 shutil.copy2 主库+WAL 三件套在宿主写入活跃时可能得到不配套的数据页
src_con = sqlite3.connect(str(REAL_DB))
dst_con = sqlite3.connect(str(copy_db))
src_con.backup(dst_con)
dst_con.close()
src_con.close()

print(f"== 演练库副本: {copy_db} ({copy_db.stat().st_size // 1024} KB) ==")

con = sqlite3.connect(str(copy_db))
n = con.execute("SELECT COUNT(*) FROM conversation_history WHERE session_id=?", (PARENT,)).fetchone()[0]
n_recall = con.execute(
    "SELECT COUNT(*) FROM conversation_history WHERE session_id=? AND name IN ('recall_history','recall_history_python')",
    (PARENT,)).fetchone()[0]
print(f"父会话 {PARENT}: {n} 行（其中 recall 工具行 {n_recall}）")
con.close()

import time
t0 = time.perf_counter()
seq_map = _copy_history_rows(copy_db, PARENT, FORK)
elapsed = time.perf_counter() - t0
print(f"\n== 复制完成: {elapsed*1000:.0f} ms, 映射 {len(seq_map)} 点 ==")

con = sqlite3.connect(str(copy_db))
fork_n = con.execute("SELECT COUNT(*) FROM conversation_history WHERE session_id=?", (FORK,)).fetchone()[0]
print(f"fork 名下: {fork_n} 行（应为 {n}）")
assert fork_n == n, "行数不一致！"

# FTS：docsize 里 fork 的行数 = n - recall 排除行
fts_fork = con.execute(
    "SELECT COUNT(*) FROM conversation_history_fts_docsize d "
    "JOIN conversation_history c ON c.seq = d.id WHERE c.session_id=?", (FORK,)).fetchone()[0]
print(f"FTS 索引: fork {fts_fork} 行（应为 {n - n_recall}）")
assert fts_fork == n - n_recall, "FTS 行数不一致！"

# FTS 索引生效验证：docsize 精确计数（MATCH 中文分词边界不可靠，不用）
# docsize 每个已索引行一条记录，计数=精确排除后的行数即索引生效
total_docsize = con.execute("SELECT COUNT(*) FROM conversation_history_fts_docsize").fetchone()[0]
print(f"FTS docsize 总行数: {total_docsize}（含父会话等原有行）")

# 幂等
seq_map2 = _copy_history_rows(copy_db, PARENT, FORK)
assert con.execute("SELECT COUNT(*) FROM conversation_history WHERE session_id=?", (FORK,)).fetchone()[0] == n
print(f"幂等二次复制: 行数不变 ✓, 映射仍 {len(seq_map2)} 点")
con.close()

# index 重映射演练
scroll = {
    "index": {
        "session_id": PARENT,
        "agent_id": "default",
        "tiers": [[{"seq_lo": min(seq_map), "seq_hi": max(seq_map), "lines": [[min(seq_map), max(seq_map), "h", "h"]]}]],
    }
}
touched = _remap_scroll_index(scroll, seq_map, FORK)
b = scroll["index"]["tiers"][0][0]
print(f"index 重映射: {touched} 点, seq_lo {min(seq_map)}→{b['seq_lo']}, seq_hi {max(seq_map)}→{b['seq_hi']}")
assert b["seq_lo"] == seq_map[min(seq_map)] and b["seq_hi"] == seq_map[max(seq_map)]

print("\n✅ 真库演练全部通过")
