"""标准库离线测试 runner：发现 tests/test_*.py 中的 test_* 函数并执行。

项目主用 pytest（pytest.ini 已配置）；本机环境没有第三方库时，
`python3 tests/run_tests.py` 提供等价的零依赖回退，断言风格两者兼容。
"""
import importlib
import inspect
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures = 0
    total = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        mod_name = "tests." + path.stem
        module = importlib.import_module(mod_name)
        for name, fn in sorted(inspect.getmembers(module, inspect.isfunction)):
            if not name.startswith("test_"):
                continue
            if getattr(fn, "__module__", "") != mod_name:
                continue
            total += 1
            try:
                fn()
            except Exception:
                failures += 1
                print(f"FAIL {mod_name}.{name}")
                traceback.print_exc()
            else:
                print(f"pass {mod_name}.{name}")
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
