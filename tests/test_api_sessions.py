"""会话端点：POST /sessions、GET /sessions、GET/PATCH/DELETE /sessions/{id}（TestClient，全离线）。

用真 create_app + 真 SQLite（落在 tmp_path），不触发 Agent 构建、不联网。

**两段式契约**（2026-09-12 起）：建会话只认 `POST /sessions`，`/chat` 的 `session_id`
是必填字段——它不再兼职建会话，名字也不再从 `/chat` 传。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import HostsSettings, Settings, StorageSettings


def _settings(tmp_path, *, enabled: bool = True) -> Settings:
    return Settings(
        storage=StorageSettings(enabled=enabled, db_path=str(tmp_path / "ops.db")),
    )


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as c:
        c.store = app.state.container.sessions()  # type: ignore[attr-defined]
    return c


@pytest.fixture
def disabled_client(tmp_path) -> TestClient:
    """持久化未启用：模拟出厂默认 / 用户显式关闭。"""
    app = create_app(settings=_settings(tmp_path, enabled=False))
    with TestClient(app) as c:
        c.path = tmp_path / "ops.db"  # type: ignore[attr-defined]
    return c


def _seed(store) -> str:
    info = store.create(user="ops")
    store.append_turn(info.session_id, user_content="CPU 怎么样", assistant_content="45%")
    return info.session_id


# -- 正常路径 ---------------------------------------------------------------
def test_list_sessions(client):
    session_id = _seed(client.store)

    resp = client.get("/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert [s["session_id"] for s in body] == [session_id]
    assert body[0]["message_count"] == 2


def test_list_sessions_respects_limit(client):
    for _ in range(3):
        _seed(client.store)

    assert len(client.get("/sessions", params={"limit": 2}).json()) == 2


def test_list_sessions_rejects_bad_limit(client):
    assert client.get("/sessions", params={"limit": 0}).status_code == 422


def test_get_session_returns_metadata_and_messages(client):
    session_id = _seed(client.store)

    resp = client.get(f"/sessions/{session_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["user"] == "ops"
    assert body["title"] == "CPU 怎么样"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "CPU 怎么样"),
        ("assistant", "45%"),
    ]


def test_get_session_shows_pending_messages(client):
    """展示口要看到 pending 行（与给模型看的窗口不同）。"""
    info = client.store.create()
    client.store.append_turn(
        info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待审批",
        status="pending_approval", approval_id="ap1",
    )

    body = client.get(f"/sessions/{info.session_id}").json()

    assert body["pending_approvals"] == 1
    assert [m["status"] for m in body["messages"]] == ["pending_approval", "pending_approval"]


def test_get_unknown_session_is_404(client):
    assert client.get("/sessions/nope").status_code == 404


def test_delete_session(client):
    session_id = _seed(client.store)

    resp = client.delete(f"/sessions/{session_id}")

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": session_id, "deleted": True, "cancelled_approvals": 0
    }
    assert client.get(f"/sessions/{session_id}").status_code == 404


def test_delete_twice_is_404(client):
    """删过的 id 再来一次是 404——**不静默重建**，否则 DELETE 形同虚设。"""
    session_id = _seed(client.store)
    client.delete(f"/sessions/{session_id}")

    assert client.delete(f"/sessions/{session_id}").status_code == 404


# -- 持久化未启用 -----------------------------------------------------------
def test_list_returns_empty_when_disabled(disabled_client):
    """沿用 GET /approvals 的约定：能力缺失 = 空列表，而非报错。"""
    resp = disabled_client.get("/sessions")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize(
    "call",
    [
        ("get", "/sessions/whatever"),
        ("delete", "/sessions/whatever"),
        ("patch", "/sessions/whatever"),
    ],
)
def test_mutations_are_503_when_disabled(disabled_client, call):
    """功能没开 ≠ 找错了 id：503 而不是 404。"""
    method, path = call
    kwargs = {"json": {"name": "x"}} if method == "patch" else {}
    assert getattr(disabled_client, method)(path, **kwargs).status_code == 503


def test_create_is_503_when_disabled(disabled_client):
    resp = disabled_client.post("/sessions", json={"name": "生产排查"})
    assert resp.status_code == 503


def test_disabled_does_not_create_db_file(disabled_client):
    """关闭时不得留下任何 db 文件（哪怕是空库）。"""
    disabled_client.get("/sessions")
    disabled_client.post("/sessions", json={})
    assert not disabled_client.path.exists()


# -- 新建会话：POST /sessions（建会话的**唯一**入口） --------------------------
def test_create_session_returns_an_id(client):
    resp = client.post("/sessions", json={})

    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"], "id 由服务端生成"
    assert body["name"] == ""
    assert body["message_count"] == 0
    assert [s["session_id"] for s in client.get("/sessions").json()] == [body["session_id"]]
    assert body["host_id"] == "legacy-default"


def test_multi_host_session_requires_explicit_host_id(tmp_path):
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(
        """
version: 1
hosts:
  - id: host-a
    name: Host A
    enabled: true
    prometheus: {job: node, instance: "a:9100"}
    allow: {}
  - id: host-b
    name: Host B
    enabled: true
    prometheus: {job: node, instance: "b:9100"}
    allow: {}
""",
        encoding="utf-8",
    )
    settings = Settings(
        storage=StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db")),
        hosts=HostsSettings(file=str(hosts_file)),
    )
    app = create_app(settings=settings)
    with TestClient(app) as multi:
        assert multi.post("/sessions", json={}).status_code == 422
        response = multi.post("/sessions", json={"host_id": "host-b"})

    assert response.status_code == 201
    assert response.json()["host_id"] == "host-b"


def test_create_session_stores_the_name_as_a_label(client):
    body = client.post("/sessions", json={"name": "生产排查"}).json()

    assert body["name"] == "生产排查"
    assert body["session_id"] != "生产排查", "名字不是 id，不该混用"


def test_duplicate_names_create_two_sessions(client):
    """名字**不参与选中会话**：同名两次 = 两段独立对话。

    如果按名字复用，调用方会以为开了一段新对话、实际接到了旧的——那正是要消除的意外。
    """
    first = client.post("/sessions", json={"name": "生产排查"}).json()
    second = client.post("/sessions", json={"name": "生产排查"}).json()

    assert first["session_id"] != second["session_id"]
    assert len(client.get("/sessions").json()) == 2


@pytest.mark.parametrize("blank", ["", "   ", "\t", None])
def test_blank_name_means_unnamed(client, blank):
    """空串 / 纯空白视同「没填」。

    这是修一个真实 bug：Swagger UI 会把**所有**字段都填进示例请求体，用户把名字那栏
    **清空**（而不是删掉）时发出去的就是 `""`，原先被判成「名字不能为空」→ 422，
    而用户的意图明明只是「我没填」。
    """
    resp = client.post("/sessions", json={"name": blank})

    assert resp.status_code == 201, f"blank={blank!r} 应视同未提供"
    assert resp.json()["name"] == ""


@pytest.mark.parametrize(
    "bad",
    [
        "a" * 65,                        # 超长
        "a" + chr(0x200B) + "b",         # 零宽空格——肉眼完全看不见，NFKC 也消不掉
        "a" + chr(0xFEFF) + "b",         # BOM
        "a" + chr(0x00) + "b",           # NUL 控制字符
        "a" + chr(0x1B) + "b",           # ESC（能注入终端转义）
        "a" + chr(0x202E) + "b",         # RTL 覆盖：能伪造视觉顺序
    ],
)
def test_invalid_name_is_422(client, bad):
    """非法名字必须在**入参校验阶段**被挡住，不能一路走到存储层再变 500。

    被拒的**只有** Cc（控制字符）与 Cf（格式字符）两类——它们的共同点是
    **肉眼看不见**（或能伪造视觉顺序），而不是「属于某种文字」。
    """
    assert client.post("/sessions", json={"name": bad}).status_code == 422


@pytest.mark.parametrize(
    "good",
    [
        "test_1",
        "生产环境 排查",   # 汉字 + 空格
        "テスト",          # 日文假名
        "café",           # 重音拉丁字母
        "生产。排查",       # 中文标点（NFKC 不动它）
        "🚀 部署排查",      # emoji
        "مرحبا",          # 阿拉伯文
        "a/b;c",          # 符号
    ],
)
def test_any_visible_text_is_accepted(client, good):
    """**不限制用哪些文字**：只按 Unicode 类别排除看不见的字符，其余一律放行。

    早先版本只允许 ASCII 加汉字，把假名、重音字母、中文标点、emoji 全误伤了，
    而拒掉这些没有任何技术依据（用户直接问「为什么中文不行」）。
    """
    assert client.post("/sessions", json={"name": good}).json()["name"] == good


@pytest.mark.parametrize(
    "variant",
    [
        "test 1",                        # 普通空格 U+0020
        "test" + chr(0xA0) + "1",        # 不换行空格 NBSP
        "test" + chr(0x3000) + "1",      # 全角空格（中文输入法下按空格很容易打出来）
        "test" + chr(10) + "1",          # 换行
        "test" + chr(9) + "1",           # 制表符
        "test  1",                       # 连续空格
        " test 1 ",                      # 首尾空格
    ],
)
def test_space_variants_normalize_to_the_same_label(client, variant):
    """空格有四种肉眼无法区分的编码，**归一化后落到同一个标签**。

    名字不用于选中会话，所以归一化的价值是：别在 `/sessions` 里看到两个**看起来
    一模一样**的标签、却没线索区分。
    """
    body = client.post("/sessions", json={"name": variant}).json()

    assert body["name"] == "test 1", f"{variant!r} 应归一到同一个标签"


def test_fullwidth_letters_normalize_to_halfwidth(client):
    """用中文输入法打出的全角字母与半角归一成同一个标签。"""
    body = client.post("/sessions", json={"name": "ｔｅｓｔ１"}).json()

    assert body["name"] == "test1"


# -- 改名：PATCH /sessions/{id} ----------------------------------------------
def test_rename_session(client):
    sid = client.post("/sessions", json={"name": "旧名"}).json()["session_id"]

    resp = client.patch(f"/sessions/{sid}", json={"name": "新名"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "新名"
    assert client.get(f"/sessions/{sid}").json()["name"] == "新名"


@pytest.mark.parametrize("blank", ["", "   "])
def test_rename_to_blank_clears_the_label(client, blank):
    """「清除」是对称操作：既然能建未命名会话，就该能退回未命名——
    否则名字只有单程票，改错了没法撤销。"""
    sid = client.post("/sessions", json={"name": "旧名"}).json()["session_id"]

    resp = client.patch(f"/sessions/{sid}", json={"name": blank})

    assert resp.status_code == 200
    assert resp.json()["name"] == ""


def test_rename_normalizes_the_new_name(client):
    sid = client.post("/sessions", json={}).json()["session_id"]

    body = client.patch(f"/sessions/{sid}", json={"name": " ａｂｃ　１ "}).json()

    assert body["name"] == "abc 1"


def test_rename_invalid_name_is_422(client):
    sid = client.post("/sessions", json={}).json()["session_id"]

    resp = client.patch(f"/sessions/{sid}", json={"name": "a" + chr(0x200B) + "b"})

    assert resp.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["name"] == "", "拒绝后不得留下半成品改名"


def test_rename_unknown_session_is_404(client):
    assert client.patch("/sessions/nope", json={"name": "x"}).status_code == 404


def test_rename_requires_a_name_field(client):
    sid = client.post("/sessions", json={}).json()["session_id"]
    assert client.patch(f"/sessions/{sid}", json={}).status_code == 422


def test_rename_keeps_messages_and_updated_at(chat_client):
    """改名只是一层薄壳：不动消息，也不动 `updated_at`（它表示「最后一句话」）。"""
    sid = _new_session(chat_client, name="原名")
    chat_client.post("/chat", json={"message": "你好", "session_id": sid})
    before = chat_client.get(f"/sessions/{sid}").json()

    body = chat_client.patch(f"/sessions/{sid}", json={"name": "新名"}).json()

    assert body["updated_at"] == before["updated_at"]
    assert body["message_count"] == before["message_count"] == 2


def test_chat_rejects_oversized_message(disabled_client):
    resp = disabled_client.post(
        "/chat",
        json={"message": "x" * 8001, "session_id": "whatever"},
    )

    assert resp.status_code == 413
    assert "超过上限" in resp.json()["detail"]


# -- /chat 多轮（M4 第二批接线） --------------------------------------------
@pytest.fixture
def chat_client(tmp_path):
    """一个装了「支持历史」的假 Agent 的 client，用于验证 /chat 的会话接线。"""
    from tests.test_chat_history import FakeHistoryAssistant

    app = create_app(settings=_settings(tmp_path))
    agent = FakeHistoryAssistant()
    app.state.agent.chat_service._assistant = agent
    with TestClient(app) as c:
        c.agent = agent  # type: ignore[attr-defined]
        c.store = app.state.container.sessions()  # type: ignore[attr-defined]
    return c


def _new_session(c: TestClient, **body) -> str:
    """走正规入口建会话——`/chat` 不再兼职建会话。"""
    return c.post("/sessions", json=body).json()["session_id"]


def test_chat_with_session_id_is_multi_turn(chat_client):
    sid = _new_session(chat_client, name="生产排查")

    first = chat_client.post("/chat", json={"message": "第一问", "session_id": sid}).json()
    second = chat_client.post("/chat", json={"message": "第二问", "session_id": sid}).json()

    assert first["session_id"] == second["session_id"] == sid
    assert second["session_name"] == "生产排查", "响应回带会话标签（只读）"
    assert [m.content for m in chat_client.agent.seen[1]] == ["第一问", "[多轮]第一问"]
    detail = chat_client.get(f"/sessions/{sid}").json()
    assert [m["content"] for m in detail["messages"]] == [
        "第一问", "[多轮]第一问", "第二问", "[多轮]第二问"
    ]


def test_chat_response_shape_is_unchanged(chat_client):
    """响应体三键没变，`session_name` 由「客户端给的别名」变成「会话的标签」。"""
    sid = _new_session(chat_client)

    body = chat_client.post("/chat", json={"message": "你好", "session_id": sid}).json()

    assert set(body) == {"reply", "session_id", "session_name"}
    assert body["session_name"] is None, "未命名会话回带 null，而不是空串"


def test_chat_without_session_id_is_422(chat_client):
    """`session_id` 是必填字段（2026-09-12 破坏性变更）。

    以前不传就新建——那让「我这次是新建还是续聊」变成调用方要猜的事，
    现在建会话只认 `POST /sessions`。
    """
    assert chat_client.post("/chat", json={"message": "你好"}).status_code == 422


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_session_id_is_422(chat_client, blank):
    """空白 id **必须报错**，不能视同「没给」而新建会话。

    字段是必填 `str`，而 pydantic 不重新校验 after-validator 的返回值——校验器若返回
    `None`，真会得到一个 `None`，接着 `if session_id:` 为假、白开一段会话，
    同时把 `DELETE` 的语义架空。这条用例锁住「宁可报错」。
    """
    resp = chat_client.post("/chat", json={"message": "你好", "session_id": blank})

    assert resp.status_code == 422
    assert chat_client.get("/sessions").json() == [], "不得留下被白建的会话"


def test_chat_with_unknown_session_id_is_404(chat_client):
    resp = chat_client.post("/chat", json={"message": "你好", "session_id": "ghost"})

    assert resp.status_code == 404, "未知 id 不得静默重建（否则 DELETE 形同虚设）"


def test_chat_trims_session_id_whitespace(chat_client):
    """id 是服务端生成的十六进制，绝不含空格——trim 只会救回「复制粘贴多带了空格」。"""
    sid = _new_session(chat_client)

    resp = chat_client.post("/chat", json={"message": "hi", "session_id": f"  {sid}  "})

    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid


def test_chat_ignores_legacy_user_and_session_name_fields(chat_client):
    """旧字段被**静默忽略**（pydantic 默认 `extra="ignore"`），不报 422。

    `user` 不再是可信输入（身份由密钥推出）；`session_name` 搬到了建会话时。
    对它们报错只会让既有调用方收到一个莫名其妙的 422，而它们在语义上已经无害。
    """
    resp = chat_client.post(
        "/chat",
        json={"user": "bob", "message": "hi", "session_id": "ghost",
              "session_name": "想改成这个"},
    )

    assert resp.status_code == 404, "走到了会话解析（而不是在校验阶段被旧字段拦下）"


def test_chat_works_when_storage_disabled(disabled_client):
    """持久化关掉时 /chat 照常回答，只是 session_id 为 null。"""
    from tests.test_chat_history import FakeHistoryAssistant

    disabled_client.app.state.agent.chat_service._assistant = FakeHistoryAssistant()
    resp = disabled_client.post("/chat", json={"message": "你好", "session_id": "whatever"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "[多轮]你好"
    assert resp.json()["session_id"] is None


# -- 归一化函数本身（直接单测，不绕 HTTP） ------------------------------------
def test_normalize_session_name_contract():
    from src.interfaces.session import normalize_session_name as norm

    assert norm("test_1") == "test_1"
    assert norm("生产环境 排查") == "生产环境 排查"
    assert norm("  a   b  ") == "a b", "去首尾 + 连续空白折成一个"
    assert norm("ａｂｃ") == "abc", "全角 → 半角"
    assert norm("test　1") == "test 1", "全角空格 → 普通空格"


@pytest.mark.parametrize(
    "bad",
    [
        "a" * 65,
        "a" + chr(0x200B) + "b",   # 零宽空格（肉眼看不见）
        "a" + chr(0xFEFF) + "b",   # BOM
        "a" + chr(0x00) + "b",     # NUL
        "a" + chr(0x1B) + "b",     # ESC
        "a" + chr(0x202E) + "b",   # RTL 覆盖
    ],
)
def test_normalize_session_name_rejects(bad):
    import pytest as _pytest

    from src.interfaces.session import normalize_session_name as norm

    with _pytest.raises(ValueError):
        norm(bad)


def test_invisible_char_error_shows_codepoint():
    """零宽空格肉眼看不见，报错必须给出码点，否则用户无从排查。"""
    import pytest as _pytest

    from src.interfaces.session import normalize_session_name as norm

    with _pytest.raises(ValueError) as exc:
        norm("a" + chr(0x200B) + "b")

    assert "U+200B" in str(exc.value)


# -- 数据真的落盘（跨 Container 实例可读） ------------------------------------
def test_data_survives_new_container(tmp_path):
    """换一个 app 实例仍能读到——这正是「持久化」与内存缓冲的区别。"""
    app1 = create_app(settings=_settings(tmp_path))
    with TestClient(app1) as c1:
        session_id = _seed(app1.state.container.sessions())

    app2 = create_app(settings=_settings(tmp_path))
    with TestClient(app2) as c2:
        resp = c2.get(f"/sessions/{session_id}")

    assert resp.status_code == 200
    assert resp.json()["message_count"] == 2


def test_delete_session_keeps_session_if_approval_lookup_fails(client, monkeypatch):
    from src.interfaces.approval import ApprovalUnavailableError

    session_id = _seed(client.store)

    def fail_lookup(*, strict=False):
        assert strict
        raise ApprovalUnavailableError("approval store unavailable")

    monkeypatch.setattr(client.app.state.agent, "pending_approvals", fail_lookup)
    response = client.delete(f"/sessions/{session_id}")
    assert response.status_code == 503
    assert client.get(f"/sessions/{session_id}").status_code == 200
