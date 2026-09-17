from scripts import demo_fault


def test_textfile_uses_lf_and_is_complete(tmp_path, monkeypatch):
    target = tmp_path / "aiops_demo_fault.prom"
    monkeypatch.setattr(demo_fault, "TARGET", target)

    demo_fault.write({"cpu": 95, "memory": 0, "disk": 0})

    payload = target.read_bytes()
    assert b"\r" not in payload
    assert b"# TYPE aiops_demo_cpu_percent gauge\n" in payload
    assert b"aiops_demo_cpu_percent 95\n" in payload
    assert not target.with_suffix(".tmp").exists()
