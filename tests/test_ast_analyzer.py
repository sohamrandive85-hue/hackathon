"""Tests for AST structural code analysis."""
from ast_analyzer import (
    parse_symbols,
    diff_python_code,
    parse_diff_lines,
    format_ast_summary,
)


def test_parse_symbols_empty():
    res = parse_symbols("")
    assert res == {"functions": {}, "classes": {}, "imports": set()}


def test_parse_symbols_python():
    code = """
import os
from math import sqrt, pi

class ModelEngine:
    def fit(self, X, y):
        pass
    def predict(self, X):
        return [0]

def train_pipeline(epochs, lr=0.01):
    pass
"""
    res = parse_symbols(code)
    assert "os" in res["imports"]
    assert "math.sqrt" in res["imports"]
    assert "ModelEngine" in res["classes"]
    assert "fit" in res["classes"]["ModelEngine"]
    assert "train_pipeline" in res["functions"]
    assert res["functions"]["train_pipeline"] == ["epochs", "lr"]


def test_diff_python_code():
    old_code = """
class PriceStore:
    def get(self, id):
        pass
"""
    new_code = """
import sqlite3

class PriceStore:
    def get(self, id, timeout=5):
        pass
    def insert(self, record):
        pass

def init_db():
    pass
"""
    diff = diff_python_code(old_code, new_code)
    assert "sqlite3" in diff["added_imports"]
    assert any("init_db" in f for f in diff["added_functions"])
    assert any("get(" in s and "timeout" in s for s in diff["modified_signatures"])


def test_parse_diff_lines():
    diff_text = """
+class Forecaster:
+    def predict(self, steps=10):
+import pyarrow.parquet
-import statsmodels
"""
    parsed = parse_diff_lines(diff_text)
    assert "Forecaster" in parsed["added_classes"]
    assert any("predict" in f for f in parsed["added_functions"])
    assert any("pyarrow" in i for i in parsed["added_imports"])
    assert any("statsmodels" in i for i in parsed["removed_imports"])


def test_format_ast_summary():
    ast_info = {
        "added_classes": ["Forecaster"],
        "added_functions": ["predict()"],
        "modified_signatures": ["fit(X) -> fit(X, y)"],
        "added_imports": ["parquet"],
        "removed_imports": ["csv"],
    }
    s = format_ast_summary(ast_info)
    assert "Classes added: Forecaster" in s
    assert "Imports added: parquet" in s
    assert "Imports removed: csv" in s
