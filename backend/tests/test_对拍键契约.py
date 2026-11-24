import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_tmp = Path(__file__).resolve().parent / "_tmp"
_tmp.mkdir(exist_ok=True)
os.environ["数据目录"] = str(_tmp)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from app import seed
from app.services import 目录
from app.modules.对拍键契约 import 服务

新契约 = {"契约版": "v2", "读列": "渠道单号", "清洗": "strip|大写|去前缀:CH-"}


def setup_function():
    db = _tmp / "对拍.db"
    if db.exists():
        db.unlink()
    seed.初始化库()
    服务.确保库()


def _切v2(请求="切-测1"):
    return 服务.切契约({**新契约, "基期版": "v1", "请求编号": 请求})


def _旧批(编号="L-旧1", 键="ORD-100", 金额=1000):
    return {
        "批次编号": "批-旧",
        "契约版": "v1",
        "事件": [{"事件编号": 编号, "业务键": 键, "金额分": 金额, "源键": "渠甲",
                  "事件时": "2026-11-24T10:00:00Z"}],
    }


def _新批(轨="左", 编号="L-新1", 单号="ch-ord-100 ", 金额=1000):
    源 = "渠甲" if 轨 == "左" else "核乙"
    return {
        "批次编号": "批-新",
        "契约版": "v2",
        "事件": [{"事件编号": 编号, "渠道单号": 单号, "金额分": 金额, "源键": 源,
                  "事件时": "2026-11-24T10:00:00Z"}],
    }


def test_切新契约后灌旧键拒收有切流():
    r = 服务.装左(_旧批())
    assert r["判定"] == "已入流" and r["现行增行"] == 1
    切 = _切v2()
    assert 切["判定"] == "已切流" and 切["自版"] == "v1"

    前 = len(服务.左轨列表())
    r = 服务.装左(_旧批(编号="L-旧2"))
    assert r["判定"] == "拒收-旧契约"
    assert r["拒因"] == "旧契约载荷"
    assert r["入流"] == 0 and r["现行增行"] == 0
    assert len(服务.左轨列表()) == 前  # 现行不增行

    账 = [x for x in 服务.账册列表() if x["类别"] == "拒收"]
    assert len(账) == 1
    assert 账[0]["拒因"] == "旧契约载荷"
    assert 账[0]["指纹"] == 切["指纹"]  # 拒收并钉契约指纹
    assert 账[0]["契约版"] == "v1"


def test_灌新键现行可对齐():
    _切v2()
    r = 服务.装左(_新批("左", "L-新1", "ch-ord-100 ", 1000))
    assert r["判定"] == "已入流"
    assert r["明细"][0]["对齐键"] == "ORD-100"  # 清洗读列抽键
    r = 服务.装右(_新批("右", "R-新1", "CH-ORD-100", 1000))
    assert r["判定"] == "已入流"
    a = 服务.跑对齐()
    assert a["契约版"] == "v2" and a["对齐"] == 1 and a["悬账"] == 0
    差 = 服务.对齐差列表()
    assert 差[0]["对齐键"] == "ORD-100" and 差[0]["契约版"] == "v2" and 差[0]["差型"] == "对齐"


def test_旧契约载荷不得静默入现行():
    _切v2()
    左前, 右前 = len(服务.左轨列表()), len(服务.右轨列表())
    r = 服务.装左(_旧批())
    assert r["现行增行"] == 0
    assert len(服务.左轨列表()) == 左前 and len(服务.右轨列表()) == 右前
    # 未申报契约版同样拒收，不默许进现行
    体 = _新批("左", "L-未申报")
    del 体["契约版"]
    r = 服务.装左(体)
    assert r["判定"] == "拒收-旧契约" and r["拒因"] == "未申报契约版"
    assert len(服务.左轨列表()) == 左前


def test_并发切契约不双成功():
    def 切(i):
        return 服务.切契约({**新契约, "基期版": "v1", "请求编号": f"并切-{i}"})

    with ThreadPoolExecutor(4) as ex:
        rs = list(ex.map(切, range(4)))
    判定 = [r["判定"] for r in rs]
    assert 判定.count("已切流") == 1  # 不加锁会双成功；加锁只一个翻牌
    assert 判定.count("基期不符") == 3
    账 = [x for x in 服务.账册列表() if x["类别"] == "切流"]
    assert len(账) == 1
    契 = [c for c in 服务.契约列表() if c["valid"]]
    assert len(契) == 1 and 契[0]["契约版"] == "v2"


def test_并发切契约同请求编号不双写():
    def 切(_):
        return 服务.切契约({**新契约, "基期版": "v1", "请求编号": "同号-1"})

    with ThreadPoolExecutor(4) as ex:
        rs = list(ex.map(切, range(4)))
    判定 = sorted(r["判定"] for r in rs)
    assert 判定.count("已切流") == 1
    assert 判定.count("幂等重放") == 3
    账 = [x for x in 服务.账册列表() if x["类别"] == "切流"]
    assert len(账) == 1  # 并发切契约会双写；请求编号唯一挡住


def test_切契约与装载并发旧键不得进现行():
    import threading

    结果 = []
    锁 = threading.Lock()

    def 装载工(i):
        for j in range(5):
            r = 服务.装左(_旧批(编号=f"PL-{i}-{j}", 键=f"K-{i}-{j}"))
            with 锁:
                结果.append(r)

    def 切换工():
        _切v2(请求="切-并发")

    线程 = [threading.Thread(target=装载工, args=(i,)) for i in range(4)]
    线程.append(threading.Thread(target=切换工))
    for t in 线程:
        t.start()
    for t in 线程:
        t.join()

    入流批 = [r for r in 结果 if r["判定"] == "已入流"]
    拒收批 = [r for r in 结果 if r["判定"] == "拒收-旧契约"]
    assert len(入流批) + len(拒收批) == 20
    for r in 拒收批:
        assert r["入流"] == 0 and r["现行增行"] == 0

    左 = 服务.左轨列表()
    assert len(左) == len(入流批)
    assert all(r["契约版"] == "v1" for r in 左)
    # 切流快照：切流事务内记录的左行数 == 终态 v1 行数（切流后旧键零增行）
    切流行 = [x for x in 服务.账册列表() if x["类别"] == "切流"][0]
    assert 切流行["摘要"]["左行数快照"] == len([r for r in 左 if r["契约版"] == "v1"])

    # 切流落定后再灌旧键：必拒，现行不增行
    r = 服务.装左(_旧批(编号="PL-后"))
    assert r["判定"] == "拒收-旧契约" and r["现行增行"] == 0
    assert len(服务.左轨列表()) == len(入流批)
    # 对齐只读现行契约键，旧契约行排除可见
    a = 服务.跑对齐()
    assert a["排除旧契约左"] == len(入流批)


def test_校验装载对齐读键同成败():
    _切v2()
    契约 = 服务.现行契约()
    坏 = {"事件编号": "L-坏", "金额分": 100, "源键": "渠甲",
          "事件时": "2026-11-24T10:00:00Z"}  # 缺读列:渠道单号
    错 = 服务.校验事件(契约, 坏)
    assert any("缺读列" in e for e in 错)
    键, 键错 = 服务.读键(契约, 坏)
    assert 键 is None and 键错 in 错  # 校验与读键同败

    好 = {"事件编号": "L-好", "渠道单号": "ch-ord-200 ", "金额分": 100, "源键": "渠甲",
          "事件时": "2026-11-24T10:00:00Z"}
    键, 键错 = 服务.读键(契约, 好)
    assert 键 == "ORD-200" and 键错 is None
    assert 服务.校验事件(契约, 好) == []

    r = 服务.装左({"批次编号": "批-混", "契约版": "v2", "事件": [坏, 好]})
    assert r["判定"] == "部分入流" and r["入流"] == 1 and r["驳回"] == 1
    左 = 服务.左轨列表()
    assert len(左) == 1 and 左[0]["事件编号"] == "L-好" and 左[0]["对齐键"] == "ORD-200"
    a = 服务.跑对齐()  # 坏读到不了对齐
    assert a["键总数"] == 1 and 服务.对齐差列表()[0]["对齐键"] == "ORD-200"


def test_契约只读接口字段():
    _切v2()
    服务.装左(_旧批())
    契 = 服务.契约列表()
    by版 = {c["契约版"]: c for c in 契}
    assert by版["v1"]["valid"] is False and by版["v2"]["valid"] is True
    for c in 契:
        assert c["指纹"] and c["读列"] and c["清洗"]
    账 = 服务.账册列表()
    拒 = [x for x in 账 if x["类别"] == "拒收"][0]
    assert 拒["拒因"] and 拒["指纹"] and 拒["契约版"]
    状 = 服务.状态()
    assert 状["现行版"] == "v2" and 状["valid"] is True
    assert 状["钉板一致"] is True and 状["拒收数"] == 1 and 状["切流数"] == 1


def test_契约冲突与已是现行():
    _切v2()
    r = 服务.切契约({"契约版": "v2", "读列": "别的列", "基期版": "v2", "请求编号": "切-冲突"})
    assert r["判定"] == "契约冲突"
    r = 服务.切契约({"契约版": "v2", "基期版": "v2", "请求编号": "切-原地"})
    assert r["判定"] == "已是现行"
    账 = [x for x in 服务.账册列表() if x["类别"] == "切流"]
    assert len(账) == 1  # 原地不切不记账


def test_切回旧版再切回新版():
    _切v2()
    r = 服务.切契约({"契约版": "v1", "基期版": "v2", "请求编号": "切-回1"})
    assert r["判定"] == "已切流" and r["读列"] == "业务键"
    r = 服务.切契约({"契约版": "v2", "基期版": "v1", "请求编号": "切-回2"})
    assert r["判定"] == "已切流" and r["读列"] == "渠道单号"
    assert 服务.状态()["现行版"] == "v2"


def test_轨规钉契约指纹():
    切 = _切v2()
    规 = 目录.轨规()
    assert 规["键契约版"] == "v2" and 规["键契约指纹"] == 切["指纹"]
    assert 服务.状态()["钉板一致"] is True


def test_策略目录():
    策 = 服务.策略目录()
    assert len(策) == 2
    现行 = [p for p in 策 if p["现行"]]
    assert len(现行) == 1 and 现行[0]["键"] == "切流账册拒旧契约"
    assert 现行[0]["要点"] == "拒收并钉契约指纹，现行不增行"


def test_api_契约接口与冲突码():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/对拍键契约/契约")
        assert r.status_code == 200 and r.json()[0]["契约版"] == "v1"
        r = c.get("/api/对拍键契约/状态")
        assert r.status_code == 200 and r.json()["现行版"] == "v1"
        r = c.get("/api/对拍键契约/策略")
        assert r.status_code == 200 and len(r.json()["策略"]) == 2
        r = c.post("/api/对拍键契约/切契约", json={**新契约, "基期版": "v9", "请求编号": "切-api"})
        assert r.status_code == 409 and r.json()["detail"]["判定"] == "基期不符"
        r = c.post("/api/对拍键契约/切契约", json={**新契约, "基期版": "v1", "请求编号": "切-api"})
        assert r.status_code == 200 and r.json()["判定"] == "已切流"
        r = c.post("/api/对拍键契约/左装载", json=_旧批())
        assert r.json()["判定"] == "拒收-旧契约" and r.json()["现行增行"] == 0
        r = c.post("/api/对拍键契约/左装载", json=_新批())
        assert r.json()["判定"] == "已入流" and r.json()["现行增行"] == 1
