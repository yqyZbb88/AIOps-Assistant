"""在已安装的离线测试镜像中校验依赖闭包与生产/测试约束，防止漏锁和陈旧约束。"""
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]


def _requirements(filename, *, follow_includes=True):
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("-r "):
            if follow_includes:
                yield from _requirements(line[3:].strip())
        elif line:
            yield Requirement(line)


def _pins(filename):
    pins = {}
    for requirement in _requirements(filename, follow_includes=False):
        specs = list(requirement.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", requirement
        name = canonicalize_name(requirement.name)
        assert name not in pins, f"重复版本约束: {name}"
        pins[name] = specs[0].version
    return pins


def _closure(filename):
    pending = list(_requirements(filename))
    visited = set()
    names = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        distribution = metadata.distribution(name)
        assert requirement.specifier.contains(distribution.version), requirement
        key = (name, frozenset(requirement.extras))
        if key in visited:
            continue
        visited.add(key)
        names.add(name)
        for raw in distribution.requires or ():
            dependency = Requirement(raw)
            if dependency.marker and not any(
                dependency.marker.evaluate({"extra": extra})
                for extra in {"", *requirement.extras}
            ):
                continue
            # 条件已在父包的 extra 上求值，后续遍历只保留依赖自身的 extras。
            dependency.marker = None
            pending.append(dependency)
    return names


def test_runtime_and_test_snapshots_cover_exact_dependency_closures():
    runtime = _pins("requirements-lock.txt")
    testing = _pins("requirements.txt")
    assert not runtime.keys() & testing.keys(), "测试约束不应重复生产约束"
    assert set(runtime) == _closure("requirements-runtime.txt")
    assert set(runtime) | set(testing) == _closure("requirements.txt")
    for name, version in (runtime | testing).items():
        assert metadata.version(name) == version, f"安装版本与约束不一致: {name}"
