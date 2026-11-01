"""Discover the two real new guard suites under the parent guarded runner."""
import importlib.util
from pathlib import Path
import sys

def load_tests(loader, tests, pattern):
    for name in ("test_guard_tools", "test_guard_controller"):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        tests.addTests(loader.loadTestsFromModule(module))
    return tests
