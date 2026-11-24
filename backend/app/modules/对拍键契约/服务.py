"""对拍键契约：字段契约变更下的入库与对齐管道。

择定策略：「切流账册拒旧契约」——拒收并钉契约指纹，现行不增行。
（备选「旧契约进只读归档新契约现行」见 策略目录()，本模块不落。）

硬推理约束落法：
- 不加锁会双成功：切契约全程持 _互斥（进程内）+ BEGIN IMMEDIATE（库级写锁），
  基期版乐观校验，检查与翻牌同一事务，两个并发切流只有一个能翻牌。
- 校验装载对齐读键同成败：校验/装载共用 读键() 按现行契约抽键；
  入流行钉契约版+指纹，对齐只读现行契约版行，抽键失败即驳回，到不了对齐。
- 旧契约载荷不得静默入现行：批次须显式申报契约版，不等于现行版整批拒收，
  写切流账册拒收行（拒因+钉现行指纹），现行流水零增行（现行增行=0 可区分）。
- 并发切契约会双写：账册 请求编号 唯一，同编号重放幂等不双写；
  键契约 现行=1 有部分唯一索引，库里至多一面现行旗。
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid

from app.db import 连接
from app.seed import 此刻
from app.services import 目录

NAME = "对拍键契约"

_互斥 = threading.Lock()

建表 = """
CREATE TABLE IF NOT EXISTS 键契约 (
  契约版 TEXT PRIMARY KEY,
  读列 TEXT NOT NULL,
  清洗 TEXT NOT NULL DEFAULT 'strip',
  指纹 TEXT NOT NULL,
  现行 INTEGER NOT NULL DEFAULT 0,
  生效时 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS 唯一现行契约 ON 键契约(现行) WHERE 现行=1;
CREATE TABLE IF NOT EXISTS 切流账册 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  类别 TEXT NOT NULL,
  契约版 TEXT NOT NULL,
  自版 TEXT NOT NULL DEFAULT '',
  指纹 TEXT NOT NULL,
  拒因 TEXT NOT NULL DEFAULT '',
  请求编号 TEXT UNIQUE,
  摘要 TEXT NOT NULL DEFAULT '{}',
  时刻 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 契约左轨 (
  事件编号 TEXT PRIMARY KEY,
  对齐键 TEXT NOT NULL,
  金额分 INTEGER NOT NULL,
  源键 TEXT NOT NULL,
  契约版 TEXT NOT NULL,
  指纹 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  处理时 TEXT NOT NULL,
  批次编号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 契约右轨 (
  事件编号 TEXT PRIMARY KEY,
  对齐键 TEXT NOT NULL,
  金额分 INTEGER NOT NULL,
  源键 TEXT NOT NULL,
  契约版 TEXT NOT NULL,
  指纹 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  处理时 TEXT NOT NULL,
  批次编号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 契约对齐差 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  契约版 TEXT NOT NULL,
  指纹 TEXT NOT NULL,
  对齐键 TEXT NOT NULL,
  左金额分 INTEGER,
  右金额分 INTEGER,
  差型 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT '',
  时刻 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 契约悬账 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  契约版 TEXT NOT NULL,
  对齐键 TEXT NOT NULL,
  悬边 TEXT NOT NULL,
  金额分 INTEGER NOT NULL,
  事件编号 TEXT NOT NULL DEFAULT '',
  原因 TEXT NOT NULL,
  时刻 TEXT NOT NULL
);
"""

_初版 = {
    "契约版": "v1",
    "读列": "业务键",
    "清洗": "strip",
    "说明": "初代键契约：直读业务键列，仅去空白",
}


def 算指纹(契约版: str, 读列: str, 清洗: str) -> str:
    料 = json.dumps(
        {"契约版": 契约版, "读列": 读列, "清洗": 清洗},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(料.encode("utf-8")).hexdigest()[:16]


def 确保库() -> None:
    conn = 连接()
    try:
        conn.executescript(建表)
        if conn.execute("SELECT COUNT(*) AS c FROM 键契约").fetchone()["c"] == 0:
            指纹 = 算指纹(_初版["契约版"], _初版["读列"], _初版["清洗"])
            conn.execute(
                "INSERT INTO 键契约(契约版,读列,清洗,指纹,现行,生效时,说明) VALUES (?,?,?,?,1,?,?)",
                (_初版["契约版"], _初版["读列"], _初版["清洗"], 指纹, 此刻(), _初版["说明"]),
            )
            conn.execute(
                "INSERT INTO 轨规(键, 值) VALUES ('键契约版', ?) "
                "ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (_初版["契约版"],),
            )
            conn.execute(
                "INSERT INTO 轨规(键, 值) VALUES ('键契约指纹', ?) "
                "ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (指纹,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------- 读键：校验/装载/对齐同一把尺 ----------

_清洗步 = ("strip", "大写", "小写")


def 解析清洗(清洗: str) -> list[str]:
    步 = [s.strip() for s in str(清洗 or "").split("|") if s.strip()]
    for s in 步:
        if s in _清洗步 or s.startswith("去前缀:") or s.startswith("去后缀:"):
            continue
        raise ValueError(f"清洗步未知:{s}")
    return 步 or ["strip"]


def 应用清洗(值: str, 清洗: str) -> str:
    out = str(值)
    for s in 解析清洗(清洗):
        if s == "strip":
            out = out.strip()
        elif s == "大写":
            out = out.upper()
        elif s == "小写":
            out = out.lower()
        elif s.startswith("去前缀:"):
            前 = s[len("去前缀:"):]
            if 前 and out.startswith(前):
                out = out[len(前):]
        elif s.startswith("去后缀:"):
            后 = s[len("去后缀:"):]
            if 后 and out.endswith(后):
                out = out[: -len(后)]
    return out


def 读键(契约: dict, ev: dict) -> tuple[str | None, str | None]:
    """按契约读列+清洗抽对齐键；校验与装载共用，失败即同败。"""
    列 = 契约["读列"]
    原 = ev.get(列)
    if 原 is None or str(原) == "":
        return None, f"缺读列:{列}"
    键 = 应用清洗(str(原), 契约["清洗"])
    if not 键:
        return None, "读列清洗后为空"
    if " " in 键 or "\t" in 键:
        return None, "对齐键含空白"
    if len(键) > 128:
        return None, "对齐键过长"
    return 键, None


def 校验事件(契约: dict, ev: dict) -> list[str]:
    """契约感知校验：与装载同读键，同成败。"""
    if not isinstance(ev, dict):
        return ["事件不是对象"]
    错: list[str] = []
    if ev.get("事件编号") in (None, ""):
        错.append("缺字段:事件编号")
    if ev.get("事件时") in (None, ""):
        错.append("缺字段:事件时")
    try:
        n = int(ev.get("金额分", 0))
        if n < 0:
            错.append("金额分不可为负")
    except (TypeError, ValueError):
        错.append("金额分非整数")
    _, 键错 = 读键(契约, ev)
    if 键错:
        错.append(键错)
    return 错


# ---------- 内部 ----------

def _行(r) -> dict:
    return dict(r)


def _现行契约(conn) -> dict:
    r = conn.execute("SELECT * FROM 键契约 WHERE 现行=1").fetchone()
    if not r:
        raise RuntimeError("无现行键契约")
    return _行(r)


def 现行契约() -> dict:
    确保库()
    conn = 连接()
    try:
        return _现行契约(conn)
    finally:
        conn.close()


def _钉轨规(conn, 契约版: str, 指纹: str) -> None:
    for k, v in (("键契约版", 契约版), ("键契约指纹", 指纹)):
        conn.execute(
            "INSERT INTO 轨规(键, 值) VALUES (?, ?) "
            "ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
            (k, v),
        )


# ---------- 切契约 ----------

def 切契约(体: dict) -> dict:
    确保库()
    新版 = str(体.get("契约版") or "").strip()
    读列 = str(体.get("读列") or "").strip()
    清洗 = str(体.get("清洗") or "strip").strip()
    基期 = 体.get("基期版")
    基期 = str(基期).strip() if 基期 not in (None, "") else None
    请求 = str(体.get("请求编号") or "").strip() or f"切-{uuid.uuid4().hex[:8]}"
    if not 新版:
        return {"判定": "参数错", "说明": "缺契约版"}
    try:
        解析清洗(清洗)
    except ValueError as e:
        return {"判定": "参数错", "说明": str(e)}

    with _互斥:
        conn = 连接()
        try:
            conn.execute("BEGIN IMMEDIATE")
            旧 = conn.execute(
                "SELECT * FROM 切流账册 WHERE 请求编号=? AND 类别='切流'", (请求,)
            ).fetchone()
            if 旧:
                conn.commit()
                return {
                    "判定": "幂等重放",
                    "契约版": 旧["契约版"],
                    "自版": 旧["自版"],
                    "指纹": 旧["指纹"],
                    "账册编号": 旧["编号"],
                    "请求编号": 请求,
                }
            现行 = _现行契约(conn)
            if 基期 is not None and 基期 != 现行["契约版"]:
                conn.rollback()
                return {
                    "判定": "基期不符",
                    "现行版": 现行["契约版"],
                    "基期版": 基期,
                    "说明": "期望现行与库中现行不一致，未翻牌",
                }
            已有 = conn.execute(
                "SELECT * FROM 键契约 WHERE 契约版=?", (新版,)
            ).fetchone()
            if 已有:
                if 读列 and (已有["读列"] != 读列 or 已有["清洗"] != 清洗):
                    conn.rollback()
                    return {
                        "判定": "契约冲突",
                        "契约版": 新版,
                        "指纹": 已有["指纹"],
                        "说明": "同版不同义：契约版已登记且定义不一致",
                    }
                读列, 清洗 = 已有["读列"], 已有["清洗"]
                if 新版 == 现行["契约版"]:
                    conn.commit()
                    return {
                        "判定": "已是现行",
                        "契约版": 新版,
                        "指纹": 已有["指纹"],
                        "说明": "目标契约已是现行，不重复记账",
                    }
            elif not 读列:
                conn.rollback()
                return {"判定": "参数错", "说明": "新契约版须给读列"}

            指纹 = 算指纹(新版, 读列, 清洗)
            if not 已有:
                conn.execute(
                    "INSERT INTO 键契约(契约版,读列,清洗,指纹,现行,生效时,说明) VALUES (?,?,?,?,0,?,?)",
                    (新版, 读列, 清洗, 指纹, 此刻(), str(体.get("说明") or "")),
                )
            左快照 = conn.execute("SELECT COUNT(*) AS c FROM 契约左轨").fetchone()["c"]
            右快照 = conn.execute("SELECT COUNT(*) AS c FROM 契约右轨").fetchone()["c"]
            conn.execute("UPDATE 键契约 SET 现行=0 WHERE 现行=1")
            conn.execute("UPDATE 键契约 SET 现行=1 WHERE 契约版=?", (新版,))
            摘要 = {"左行数快照": 左快照, "右行数快照": 右快照}
            cur = conn.execute(
                "INSERT INTO 切流账册(类别,契约版,自版,指纹,请求编号,摘要,时刻) "
                "VALUES ('切流',?,?,?,?,?,?)",
                (新版, 现行["契约版"], 指纹, 请求, json.dumps(摘要, ensure_ascii=False), 此刻()),
            )
            _钉轨规(conn, 新版, 指纹)
            conn.commit()
            return {
                "判定": "已切流",
                "契约版": 新版,
                "自版": 现行["契约版"],
                "读列": 读列,
                "清洗": 清洗,
                "指纹": 指纹,
                "账册编号": cur.lastrowid,
                "请求编号": 请求,
            }
        finally:
            conn.close()


# ---------- 装载：旧契约整批拒收，现行不增行 ----------

def _装(轨别: str, 体: dict) -> dict:
    确保库()
    表 = "契约左轨" if 轨别 == "左" else "契约右轨"
    默认源 = "渠甲" if 轨别 == "左" else "核乙"
    事件 = 体.get("事件") or []
    批次 = str(体.get("批次编号") or "").strip() or f"契{轨别}批-{uuid.uuid4().hex[:8]}"
    申报 = 体.get("契约版")
    申报 = str(申报).strip() if 申报 not in (None, "") else ""

    with _互斥:
        conn = 连接()
        try:
            conn.execute("BEGIN IMMEDIATE")
            现行 = _现行契约(conn)
            报告 = {
                "批次编号": 批次,
                "轨别": 轨别,
                "契约版": 申报 or "(未申报)",
                "现行版": 现行["契约版"],
                "指纹": 现行["指纹"],
                "判定": "",
                "入流": 0,
                "跳过": 0,
                "驳回": 0,
                "现行增行": 0,
                "明细": [],
            }
            if not isinstance(事件, list) or not 事件:
                报告["判定"] = "拒收-空批"
                报告["明细"].append({"命运": "驳回-校验", "说明": "事件列表为空"})
                conn.commit()
                return 报告

            if 申报 != 现行["契约版"]:
                if not 申报:
                    拒因 = "未申报契约版"
                elif conn.execute(
                    "SELECT 1 FROM 键契约 WHERE 契约版=?", (申报,)
                ).fetchone():
                    拒因 = "旧契约载荷"
                else:
                    拒因 = "契约版未登记"
                请求 = str(体.get("请求编号") or "").strip() or f"拒-{uuid.uuid4().hex[:8]}"
                旧 = conn.execute(
                    "SELECT * FROM 切流账册 WHERE 请求编号=?", (请求,)
                ).fetchone()
                if 旧:
                    conn.commit()
                    报告["判定"] = "拒收-旧契约(幂等重放)"
                    报告["拒因"] = 旧["拒因"]
                    报告["账册编号"] = 旧["编号"]
                    return 报告
                摘要 = {
                    "批次编号": 批次,
                    "轨别": 轨别,
                    "事件数": len(事件),
                    "事件编号": [str((e or {}).get("事件编号", "")) for e in 事件][:50],
                }
                cur = conn.execute(
                    "INSERT INTO 切流账册(类别,契约版,自版,指纹,拒因,请求编号,摘要,时刻) "
                    "VALUES ('拒收',?,?,?,?,?,?,?)",
                    (申报 or "(未申报)", 现行["契约版"], 现行["指纹"], 拒因, 请求,
                     json.dumps(摘要, ensure_ascii=False), 此刻()),
                )
                conn.commit()
                报告["判定"] = "拒收-旧契约"
                报告["拒因"] = 拒因
                报告["账册编号"] = cur.lastrowid
                return 报告

            for ev in 事件:
                错 = 校验事件(现行, ev if isinstance(ev, dict) else {})
                编号 = str((ev or {}).get("事件编号") or "")
                if 错:
                    报告["驳回"] += 1
                    报告["明细"].append(
                        {"事件编号": 编号, "命运": "驳回-校验", "说明": ";".join(错)}
                    )
                    continue
                if conn.execute(
                    f"SELECT 1 FROM {表} WHERE 事件编号=?", (编号,)
                ).fetchone():
                    报告["跳过"] += 1
                    报告["明细"].append({"事件编号": 编号, "命运": "幂等跳过"})
                    continue
                源 = str(ev.get("源键") or 默认源)
                if not conn.execute(
                    "SELECT 1 FROM 源登记 WHERE 源键=? AND 轨别=?", (源, 轨别)
                ).fetchone():
                    报告["驳回"] += 1
                    报告["明细"].append({"事件编号": 编号, "命运": f"驳回-未知{轨别}源"})
                    continue
                键, _ = 读键(现行, ev)
                金额 = int(ev.get("金额分", 0))
                事件时 = str(ev.get("事件时") or 此刻())
                conn.execute(
                    f"INSERT INTO {表}(事件编号,对齐键,金额分,源键,契约版,指纹,事件时,处理时,批次编号) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (编号, 键, 金额, 源, 现行["契约版"], 现行["指纹"], 事件时, 此刻(), 批次),
                )
                报告["入流"] += 1
                报告["明细"].append({"事件编号": 编号, "对齐键": 键, "命运": f"已入{轨别}"})
            报告["现行增行"] = 报告["入流"]
            if 报告["入流"] == len(事件):
                报告["判定"] = "已入流"
            elif 报告["入流"] > 0 or 报告["跳过"] > 0:
                报告["判定"] = "部分入流"
            else:
                报告["判定"] = "全驳"
            conn.commit()
            return 报告
        finally:
            conn.close()


def 装左(体: dict) -> dict:
    return _装("左", 体)


def 装右(体: dict) -> dict:
    return _装("右", 体)


# ---------- 对齐：只读现行契约版行 ----------

def 跑对齐() -> dict:
    确保库()
    规 = 目录.轨规()
    容差 = int(规.get("金额容差分", "0"))
    with _互斥:
        conn = 连接()
        try:
            conn.execute("BEGIN IMMEDIATE")
            现行 = _现行契约(conn)
            版, 指纹 = 现行["契约版"], 现行["指纹"]
            conn.execute("DELETE FROM 契约对齐差 WHERE 契约版=?", (版,))
            conn.execute("DELETE FROM 契约悬账 WHERE 契约版=?", (版,))

            左汇总: dict[str, int] = {}
            左代表: dict[str, str] = {}
            for r in conn.execute(
                "SELECT * FROM 契约左轨 WHERE 契约版=?", (版,)
            ).fetchall():
                k = r["对齐键"]
                左汇总[k] = 左汇总.get(k, 0) + int(r["金额分"])
                左代表[k] = r["事件编号"]
            右汇总: dict[str, int] = {}
            右代表: dict[str, str] = {}
            for r in conn.execute(
                "SELECT * FROM 契约右轨 WHERE 契约版=?", (版,)
            ).fetchall():
                k = r["对齐键"]
                右汇总[k] = 右汇总.get(k, 0) + int(r["金额分"])
                右代表[k] = r["事件编号"]

            旧左 = conn.execute(
                "SELECT COUNT(*) AS c FROM 契约左轨 WHERE 契约版<>?", (版,)
            ).fetchone()["c"]
            旧右 = conn.execute(
                "SELECT COUNT(*) AS c FROM 契约右轨 WHERE 契约版<>?", (版,)
            ).fetchone()["c"]

            时刻 = 此刻()
            对齐 = 差 = 悬 = 0
            for k in sorted(set(左汇总) | set(右汇总)):
                有左, 有右 = k in 左汇总, k in 右汇总
                if 有左 and 有右:
                    la, ra = 左汇总[k], 右汇总[k]
                    if abs(la - ra) <= 容差:
                        差型, 说明 = "对齐", "双边金额在容差内"
                        对齐 += 1
                    else:
                        差型, 说明 = "金额不符", f"左{la}-右{ra}"
                        差 += 1
                    conn.execute(
                        "INSERT INTO 契约对齐差(契约版,指纹,对齐键,左金额分,右金额分,差型,说明,时刻) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (版, 指纹, k, la, ra, 差型, 说明, 时刻),
                    )
                elif 有左:
                    conn.execute(
                        "INSERT INTO 契约对齐差(契约版,指纹,对齐键,左金额分,右金额分,差型,说明,时刻) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (版, 指纹, k, 左汇总[k], None, "右缺", "仅左轨有键", 时刻),
                    )
                    conn.execute(
                        "INSERT INTO 契约悬账(契约版,对齐键,悬边,金额分,事件编号,原因,时刻) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (版, k, "左", 左汇总[k], 左代表.get(k, ""), "右轨缺键", 时刻),
                    )
                    差 += 1
                    悬 += 1
                else:
                    conn.execute(
                        "INSERT INTO 契约对齐差(契约版,指纹,对齐键,左金额分,右金额分,差型,说明,时刻) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (版, 指纹, k, None, 右汇总[k], "左缺", "仅右轨有键", 时刻),
                    )
                    conn.execute(
                        "INSERT INTO 契约悬账(契约版,对齐键,悬边,金额分,事件编号,原因,时刻) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (版, k, "右", 右汇总[k], 右代表.get(k, ""), "左轨缺键", 时刻),
                    )
                    差 += 1
                    悬 += 1
            conn.commit()
            return {
                "契约版": 版,
                "指纹": 指纹,
                "键总数": len(set(左汇总) | set(右汇总)),
                "对齐": 对齐,
                "差与悬": 差,
                "悬账": 悬,
                "容差分": 容差,
                "排除旧契约左": 旧左,
                "排除旧契约右": 旧右,
                "时刻": 时刻,
            }
        finally:
            conn.close()


# ---------- 只读接口 ----------

def 契约列表() -> list[dict]:
    确保库()
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT 契约版,读列,清洗,指纹,现行,生效时,说明 FROM 键契约 ORDER BY 生效时, 契约版"
        ).fetchall()
        out = []
        for r in rows:
            d = _行(r)
            d["valid"] = bool(d["现行"])
            out.append(d)
        return out
    finally:
        conn.close()


def 账册列表() -> list[dict]:
    确保库()
    conn = 连接()
    try:
        rows = conn.execute("SELECT * FROM 切流账册 ORDER BY 编号 DESC").fetchall()
        out = []
        for r in rows:
            d = _行(r)
            try:
                d["摘要"] = json.loads(d.get("摘要") or "{}")
            except (TypeError, ValueError):
                pass
            out.append(d)
        return out
    finally:
        conn.close()


def 状态() -> dict:
    确保库()
    conn = 连接()
    try:
        现行 = _现行契约(conn)
        规 = 目录.轨规()
        钉版 = 规.get("键契约版", "")
        钉指纹 = 规.get("键契约指纹", "")
        c = lambda sql, *a: conn.execute(sql, a).fetchone()["c"]
        最近切流 = conn.execute(
            "SELECT * FROM 切流账册 WHERE 类别='切流' ORDER BY 编号 DESC LIMIT 1"
        ).fetchone()
        return {
            "现行版": 现行["契约版"],
            "指纹": 现行["指纹"],
            "读列": 现行["读列"],
            "清洗": 现行["清洗"],
            "valid": True,
            "轨规钉版": 钉版,
            "轨规钉指纹": 钉指纹,
            "钉板一致": 钉版 == 现行["契约版"] and 钉指纹 == 现行["指纹"],
            "左行数": c("SELECT COUNT(*) AS c FROM 契约左轨"),
            "右行数": c("SELECT COUNT(*) AS c FROM 契约右轨"),
            "左旧契约行数": c("SELECT COUNT(*) AS c FROM 契约左轨 WHERE 契约版<>?", 现行["契约版"]),
            "右旧契约行数": c("SELECT COUNT(*) AS c FROM 契约右轨 WHERE 契约版<>?", 现行["契约版"]),
            "切流数": c("SELECT COUNT(*) AS c FROM 切流账册 WHERE 类别='切流'"),
            "拒收数": c("SELECT COUNT(*) AS c FROM 切流账册 WHERE 类别='拒收'"),
            "最近切流": _行(最近切流) if 最近切流 else None,
        }
    finally:
        conn.close()


def 策略目录() -> list[dict]:
    return [
        {
            "键": "切流账册拒旧契约",
            "形态": "拒收",
            "现行": True,
            "要点": "拒收并钉契约指纹，现行不增行",
            "详情": {
                "入库": "批次申报契约版≠现行版 → 整批拒收，写切流账册拒收行并钉现行指纹",
                "现行流水": "不增行：回执 现行增行=0，与已入流可区分",
                "读引用": "校验/装载/对齐同读现行契约键（读列+清洗），同成败",
                "并发": "互斥锁+BEGIN IMMEDIATE+基期版校验不双成功；请求编号唯一不双写",
            },
        },
        {
            "键": "旧契约进只读归档新契约现行",
            "形态": "改读",
            "现行": False,
            "要点": "旧行归档，现行只读新契约键；读引用不同",
            "详情": {
                "入库": "旧契约载荷改读入只读归档，不进现行流水",
                "现行流水": "只读新契约键；旧行以归档键引用",
                "读引用": "归档引用与现行引用不同，查询须显式择轨",
                "并发": "切契约与归档迁移须同事务，否则半截归档",
            },
        },
    ]


def 左轨列表() -> list[dict]:
    确保库()
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT * FROM 契约左轨 ORDER BY 处理时 DESC LIMIT 300"
        ).fetchall()
        return [_行(r) for r in rows]
    finally:
        conn.close()


def 右轨列表() -> list[dict]:
    确保库()
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT * FROM 契约右轨 ORDER BY 处理时 DESC LIMIT 300"
        ).fetchall()
        return [_行(r) for r in rows]
    finally:
        conn.close()


def 对齐差列表() -> list[dict]:
    确保库()
    conn = 连接()
    try:
        rows = conn.execute("SELECT * FROM 契约对齐差 ORDER BY 编号 DESC").fetchall()
        return [_行(r) for r in rows]
    finally:
        conn.close()


def 悬账列表() -> list[dict]:
    确保库()
    conn = 连接()
    try:
        rows = conn.execute("SELECT * FROM 契约悬账 ORDER BY 编号 DESC").fetchall()
        return [_行(r) for r in rows]
    finally:
        conn.close()
