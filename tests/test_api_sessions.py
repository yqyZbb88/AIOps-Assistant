"""会话端点：GET /sessions、GET /sessions/{id}、DELETE /sessions/{id}（TestClient，全离线）。

用真 create_app + 真 SQLite（落在 tmp_path），不触发 Agent 构建、不联网。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import Settings, StorageSettings


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


def test_detail_is_503_when_disabled(disabled_client):
    assert disabled_client.get("/sessions/whatever").status_code == 503


def test_delete_is_503_when_disabled(disabled_client):
    assert disabled_client.delete("/sessions/whatever").status_code == 503


def test_disabled_does_not_create_db_file(disabled_client):
    """关闭时不得留下任何 db 文件（哪怕是空库）。"""
    disabled_client.get("/sessions")
    assert not disabled_client.path.exists()


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


def test_chat_creates_session_and_returns_id(chat_client):
    resp = chat_client.post("/chat", json={"user": "ops", "message": "CPU 怎么样"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"reply", "session_id", "session_name"}
    assert body["session_id"], "不传 session_id 时应新建并在响应里回带"
    assert [s["session_id"] for s in chat_client.get("/sessions").json()] == [body["session_id"]]


def test_chat_with_session_id_is_multi_turn(chat_client):
    first = chat_client.post("/chat", json={"user": "ops", "message": "第一问"}).json()

    second = chat_client.post(
        "/chat", json={"user": "ops", "message": "第二问", "session_id": first["session_id"]}
    ).json()

    assert second["session_id"] == first["session_id"], "同一会话不应换 id"
    assert [m.content for m in chat_client.agent.seen[1]] == ["第一问", "[多轮]第一问"]
    detail = chat_client.get(f"/sessions/{first['session_id']}").json()
    assert [m["content"] for m in detail["messages"]] == [
        "第一问", "[多轮]第一问", "第二问", "[多轮]第二问"
    ]


def test_chat_without_session_id_starts_new_each_time(chat_client):
    a = chat_client.post("/chat", json={"user": "ops", "message": "问一"}).json()
    b = chat_client.post("/chat", json={"user": "ops", "message": "问二"}).json()

    assert a["session_id"] != b["session_id"]
    assert len(chat_client.get("/sessions").json()) == 2


def test_chat_with_unknown_session_id_is_404(chat_client):
    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "你好", "session_id": "ghost"}
    )

    assert resp.status_code == 404, "未知 id 不得静默重建（否则 DELETE 形同虚设）"


def test_chat_works_when_storage_disabled(disabled_client):
    """持久化关掉时 /chat 照常回答，只是 session_id 为 null。"""
    from tests.test_chat_history import FakeHistoryAssistant

    disabled_client.app.state.agent.chat_service._assistant = FakeHistoryAssistant()
    resp = disabled_client.post("/chat", json={"user": "ops", "message": "你好"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "[多轮]你好"
    assert resp.json()["session_id"] is None


# -- /chat 会话名（**纯标签**，不参与选中会话） --------------------------------
def test_chat_with_session_name_labels_the_new_session(chat_client):
    """没有 id 时新建一段对话，名字作为标签记下来，供人在 /sessions 里识别。"""
    body = chat_client.post(
        "/chat", json={"user": "yqyZ", "message": "cpu", "session_name": "生产排查"}
    ).json()

    assert body["session_name"] == "生产排查"
    assert body["session_id"], "选中会话仍靠服务端生成的随机 id"
    assert body["session_id"] != "生产排查", "名字不是 id，不该混用"
    listed = chat_client.get("/sessions").json()
    assert [(s["name"], s["session_id"]) for s in listed] == [
        ("生产排查", body["session_id"])
    ]


def test_same_name_twice_creates_two_sessions(chat_client):
    """**这是方案变更的核心后果**：名字不参与选中，同名请求两次 = 两段新对话。

    旧行为（名字是别名）会接回同一段对话。改成标签后，续聊**只能**靠 id——
    否则调用方以为开了一段新对话、实际接到了旧的，那正是要消除的意外。
    """
    first = chat_client.post(
        "/chat", json={"user": "yqyZ", "message": "第一问", "session_name": "生产排查"}
    ).json()

    second = chat_client.post(
        "/chat", json={"user": "yqyZ", "message": "第二问", "session_name": "生产排查"}
    ).json()

    assert second["session_id"] != first["session_id"]
    assert chat_client.agent.seen[1] == [], "新会话不该带上另一段的上下文"
    assert len(chat_client.get("/sessions").json()) == 2
    assert [s["name"] for s in chat_client.get("/sessions").json()] == ["生产排查"] * 2


def test_name_is_ignored_when_session_id_is_given(chat_client):
    """同时给了 id 和名字 → **名字被忽略**，不产生任何副作用。

    `/chat` 只管聊天；给已有会话改名应当走专门的操作，
    不该让「发一句话」顺带改掉标签。
    """
    first = chat_client.post(
        "/chat", json={"user": "ops", "message": "一", "session_name": "原名"}
    ).json()

    again = chat_client.post(
        "/chat",
        json={"user": "ops", "message": "二", "session_id": first["session_id"],
              "session_name": "想改成这个"},
    )

    assert again.status_code == 200, "不该再因为「两个都给」报错"
    assert again.json()["session_id"] == first["session_id"]
    assert again.json()["session_name"] == "原名", "标签没被这次请求改掉"
    detail = chat_client.get(f"/sessions/{first['session_id']}").json()
    assert detail["name"] == "原名"


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
def test_invalid_session_name_is_422(chat_client, bad):
    """非法名字必须在**入参校验阶段**被挡住，不能一路走到存储层再变 500。

    被拒的**只有** Cc（控制字符）与 Cf（格式字符）两类——它们的共同点是
    **肉眼看不见**（或能伪造视觉顺序），而不是「属于某种文字」。
    """
    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_name": bad}
    )

    assert resp.status_code == 422


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
def test_any_visible_text_is_accepted(chat_client, good):
    """**不限制用哪些文字**：只按 Unicode 类别排除看不见的字符，其余一律放行。

    早先版本只允许 ASCII 加汉字，把假名、重音字母、中文标点、emoji 全误伤了，
    而拒掉这些没有任何技术依据（用户直接问「为什么中文不行」）。
    """
    body = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_name": good}
    ).json()

    assert body["session_name"] == good


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
def test_space_variants_normalize_to_the_same_label(chat_client, variant):
    """空格有四种肉眼无法区分的编码，**归一化后落到同一个标签**。

    名字不再用于选中会话，所以归一化的价值从「别开成两段对话」变成了
    「别在 `/sessions` 里看到两个**看起来一模一样**的标签、却没线索区分」。
    """
    body = chat_client.post(
        "/chat", json={"user": "ops", "message": "一", "session_name": variant}
    ).json()

    assert body["session_name"] == "test 1", f"{variant!r} 应归一到同一个标签"


def test_fullwidth_letters_normalize_to_halfwidth(chat_client):
    """用中文输入法打出的全角字母与半角归一成同一个标签。"""
    body = chat_client.post(
        "/chat", json={"user": "ops", "message": "一", "session_name": "ｔｅｓｔ１"}
    ).json()

    assert body["session_name"] == "test1"


def test_session_name_absent_returns_null(chat_client):
    """不传名字时响应里是 null，不是空串——空串会被误当成「有个名字叫空」。"""
    body = chat_client.post("/chat", json={"user": "ops", "message": "hi"}).json()

    assert body["session_name"] is None
    assert body["session_id"]


def test_unknown_session_id_still_404_even_with_name_support(chat_client):
    """加了名字之后，未知 id 的语义不变：仍然 404，不静默重建。"""
    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_id": "ghost"}
    )

    assert resp.status_code == 404


# -- 空串 / 纯空白视同「未提供」 ---------------------------------------------
def _new_session(chat_client) -> str:
    return chat_client.post("/chat", json={"user": "ops", "message": "开始"}).json()["session_id"]


@pytest.mark.parametrize("blank", ["", "   ", "\t", None])
def test_blank_session_name_means_absent(chat_client, blank):
    """`session_name` 为空时按「没填」处理，而不是拿它去校验。

    这是修一个真实 bug：Swagger UI 会把**所有**字段都填进示例请求体，
    用户把 `session_name` 那栏**清空**（而不是删掉）时发出去的就是 `""`，
    原先会被判成「名字不能为空」→ 422。而用户的意图明明只是「我没填名字」。
    """
    sid = _new_session(chat_client)

    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_id": sid, "session_name": blank}
    )

    assert resp.status_code == 200, f"blank={blank!r} 应视同未提供"
    assert resp.json()["session_id"] == sid, "应继续用 session_id 指定的会话"
    assert resp.json()["session_name"] is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_session_id_means_absent(chat_client, blank):
    """`session_id` 为空 → 视同没给 → 走「都没给 → 新建会话」这条明确路径。

    （行为结果与修复前一样，区别在于它现在是一条说得通的路径，
    而不是「把一个空串当成了某个 id」。且不能报错——Swagger 清空输入框就会产生空串。）
    """
    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_id": blank}
    )

    assert resp.status_code == 200
    assert resp.json()["session_id"], "应新建一个会话"


def test_session_id_surrounding_whitespace_is_trimmed(chat_client):
    """id 是服务端生成的十六进制，绝不含空格——去掉首尾空白只会救回复制粘贴多带空格。"""
    sid = _new_session(chat_client)

    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_id": f"  {sid}  "}
    )

    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid


def test_blank_name_with_session_id_is_fine(chat_client):
    """`session_id` + 空白名字 → 正常续聊（空白视同未提供，且名字本来就被忽略）。"""
    sid = _new_session(chat_client)

    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "hi", "session_id": sid, "session_name": "  "}
    )

    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid


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
        norm("a\u200bb")

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
